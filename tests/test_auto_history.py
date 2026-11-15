"""Offline regression tests for automated Base evidence history."""
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'engine'))
from refresh_site import (EvidenceError, SCHEMA, validate_public, validate_manifest,
                          history_from_site, capture_matured, append_snapshot, build_outputs,
                          refresh, make_chart, to_engine, write_outputs, utc)
from publish_site import validate_live, stable_json
from stock_integrity import ASSETS, BASE_ASSETS_SOURCE


def sample(height, minutes_ago=20, supply=10_000_000, hash_mark='a', code_mark='b'):
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    block_time = stamp - timedelta(seconds=12)
    full = {'schema_version':'equity-lens-2', 'chain_id':8453,
            'mode':'LIVE_BASE_MAINNET_READ_ONLY', 'source_url':BASE_ASSETS_SOURCE,
            'captured_at_utc':stamp.isoformat(),
            'block':{'number':height, 'hash':'0x'+hash_mark*64,
                     'block_timestamp_utc':block_time.isoformat()},
            'assets':[
                {'ticker':ticker,'address':addr,'status':'READ_OK','code_sha256':code_mark*64,
                 'code_bytes':2,'total_supply_raw':str(supply*(i+1)),
                 'decimals_reported':6,'token_units_reported':str((supply*(i+1))//1_000_000)}
                for i,(ticker,addr) in enumerate(ASSETS.items())]}
    return validate_live(full)


def site(root, two=None):
    a,b = two or (sample(100,20),sample(101,10,hash_mark='c'))
    data = {'earlier.json':stable_json(a), 'later.json':stable_json(b),
            'comparison.json':stable_json({'not_used_in_v3_refresh':'but hash-verified'})}
    (root/'data').mkdir(parents=True,exist_ok=True)
    for key,v in data.items(): (root/'data'/key).write_bytes(v)
    manifest={'schema_version':'equity-lens-public-manifest-1', 'chain_id':8453,
              'files_sha256':{k:hashlib.sha256(v).hexdigest() for k,v in data.items()}}
    (root/'data/manifest.json').write_bytes(stable_json(manifest))
    (root/'index.html').write_text('Initial published site (not touched unless capture succeeds)')
    return a,b

class BaseHistoryTests(unittest.TestCase):
    def test_public_data_validation(self):
        s=sample(1)
        self.assertEqual(validate_public(s)['block']['number'],1)
        self.assertEqual(to_engine(s)['schema_version'],'equity-lens-2')
    def test_synthetic_rejected(self):
        s=sample(1); s['mode']='OFFLINE_DEMO_SYNTHETIC'
        with self.assertRaises(EvidenceError):validate_public(s)
    def test_wrong_chain_rejected(self):
        s=sample(1); s['chain_id']=1
        with self.assertRaises(EvidenceError):validate_public(s)
    def test_wrong_address_rejected(self):
        s=sample(1);s['assets'][0]['address']=ASSETS['NVDAc']
        with self.assertRaises(EvidenceError):validate_public(s)
    def test_incomplete_asset_rejected(self):
        s=sample(1);s['assets'][1]['status']='NOT_VERIFIED'
        with self.assertRaises(EvidenceError):validate_public(s)
    def test_supply_value_forgery_rejected(self):
        s=sample(1);s['assets'][0]['token_units_reported']='12345'
        with self.assertRaises(EvidenceError):validate_public(s)
    def test_invalid_hash_rejected(self):
        s=sample(1);s['block']['hash']='evil'
        with self.assertRaises(EvidenceError):validate_public(s)
    def test_manifest_integrity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);site(root)
            self.assertEqual(len(history_from_site(root)),2)
            (root/'data/later.json').write_text('{"faked":true}')
            with self.assertRaisesRegex(EvidenceError,'HASH_MISMATCH'):history_from_site(root)
    def test_check_only_never_network_or_changes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);site(root)
            def forbidden(): raise AssertionError('Never call network')
            original=(root/'index.html').read_bytes()
            out=refresh(root,capture_fn=forbidden,check_only=True)
            self.assertFalse(out['written']);self.assertEqual((root/'index.html').read_bytes(),original)
    def test_append_strict_height(self):
        a,b=sample(100),sample(101,hash_mark='c')
        x,yes=append_snapshot([a,b], sample(102,minutes_ago=1,hash_mark='d'))
        self.assertTrue(yes);self.assertEqual(len(x),3)
        with self.assertRaises(EvidenceError):append_snapshot([a,b],sample(99,hash_mark='e'))
        x,no=append_snapshot([a,b],b)
        self.assertFalse(no)
    def test_same_block_different_hash_rejected(self):
        a,b=sample(100),sample(101,hash_mark='c')
        with self.assertRaises(EvidenceError):append_snapshot([a,b],sample(101,hash_mark='d'))
    def test_not_newest_time_rejected(self):
        a,b=sample(100,minutes_ago=5),sample(101,minutes_ago=4,hash_mark='c')
        with self.assertRaises(EvidenceError):append_snapshot([a,b],sample(102,minutes_ago=6,hash_mark='d'))
    def test_full_refresh_offline_no_wallet(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);site(root)
            new=sample(102,minutes_ago=1,hash_mark='d',supply=11_000_000)
            out=refresh(root,capture_fn=lambda:new)
            self.assertEqual(out['history_samples'],3)
            self.assertTrue(out['written'])
            self.assertEqual(history_from_site(root)[-1]['block']['number'],102)
            page=(root/'index.html').read_text()
            for marker in ('Gözlenen arz geçmişi','data/history.json','READ ONLY','sıfırdan başlamaz'):
                self.assertIn(marker,page)
            self.assertIn('<svg',page)
            self.assertIn('Son başarılı gözlem',page)
            self.assertNotIn('Yakın zamanda alınmış kayıt',page)
            self.assertEqual(validate_manifest(root)['chain_id'],8453)
            self.assertEqual(len(json.loads((root/'data/history.json').read_text())['snapshots']),3)
    def test_malformed_new_data_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);site(root)
            previous=(root/'data/later.json').read_bytes();page=(root/'index.html').read_bytes()
            bad=sample(102,minutes_ago=1,hash_mark='d')
            bad['assets'][1]['status']='NOT_VERIFIED'
            with self.assertRaises(EvidenceError):refresh(root,capture_fn=lambda:bad)
            self.assertEqual(previous,(root/'data/later.json').read_bytes())
            self.assertEqual(page,(root/'index.html').read_bytes())
            self.assertFalse((root/'data/history.json').exists())
    def test_stale_source_bundle_without_manifest_history_not_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);site(root)
            (root/'data/history.json').write_text('{}')
            with self.assertRaises(EvidenceError):history_from_site(root)
    def test_code_hash_changes_review_not_fraud(self):
        s=[sample(100,20),sample(101,10,hash_mark='c',code_mark='d')]
        out=build_outputs(s)
        report=json.loads(out['data/comparison.json'])
        self.assertEqual(report['results'][0]['status'],'CHANGE_REVIEW')
        self.assertIn('RUNTIME_CODE_SHA256_CHANGED',report['results'][0]['changes'])
        self.assertIn('güvenlik sertifikası',out['index.html'].decode())
    def test_decimals_change_suppresses_chart(self):
        a,b=sample(100),sample(101,hash_mark='c')
        b['assets'][0]['decimals_reported']=4
        b['assets'][0]['token_units_reported']='1000'
        self.assertIn('line suppressed',make_chart('AAPLc',[a,b]))
    def test_no_untrusted_html_in_chart(self):
        result=make_chart('AAPLc',[sample(100),sample(101,hash_mark='c')])
        self.assertNotIn('<script',result)
    def test_save_outputs_integrity(self):
        a,b=sample(100),sample(101,hash_mark='c')
        result=build_outputs([a,b]);self.assertEqual(len(result),7)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);write_outputs(p,result)
            self.assertEqual(validate_manifest(p)['chain_id'],8453)
            self.assertTrue((p/'.nojekyll').exists())
    def test_capture_rechecks_header_and_no_tx(self):
        now=datetime.now(timezone.utc)
        class Transport:
            def __init__(self):self.methods=[];self.header_calls=0
            def __call__(self,url,p):
                method=p['method'];self.methods.append(method)
                if method=='eth_chainId': result='0x2105'
                elif method=='eth_blockNumber': result=hex(132)
                elif method=='eth_getBlockByNumber':
                    self.header_calls+=1
                    result={'number':'0x64','hash':'0x'+'e'*64,
                            'timestamp':hex(int(now.timestamp())-60)}
                elif method=='eth_getCode':result='0x6001'
                elif method=='eth_call':
                    result='0x'+format(10_000_000 if p['params'][0]['data']=='0x18160ddd' else 6,'064x')
                else:raise AssertionError(method)
                return {'jsonrpc':'2.0','id':1,'result':result}
        t=Transport();x=capture_matured(transport=t,now=now)
        self.assertEqual(x['block']['number'],100)
        self.assertEqual(t.header_calls,2)
        self.assertTrue(set(t.methods).issubset({'eth_chainId','eth_blockNumber','eth_getBlockByNumber','eth_getCode','eth_call'}))
        self.assertTrue(all(a['status']=='READ_OK' for a in x['assets']))
    def test_capture_reorg_rejected(self):
        now=datetime.now(timezone.utc)
        class Transport:
            def __init__(self):self.headers=0
            def __call__(self,url,p):
                m=p['method'];result=None
                if m=='eth_chainId': result='0x2105'
                elif m=='eth_blockNumber':result=hex(132)
                elif m=='eth_getBlockByNumber':
                    self.headers+=1
                    result={'number':'0x64','hash':'0x'+('a' if self.headers==1 else 'b')*64,
                            'timestamp':hex(int(now.timestamp())-60)}
                elif m=='eth_getCode':result='0x6001'
                elif m=='eth_call':result='0x'+format(10_000_000 if p['params'][0]['data']=='0x18160ddd' else 6,'064x')
                return {'jsonrpc':'2.0','id':1,'result':result}
        with self.assertRaisesRegex(EvidenceError,'Block identity changed'):
            capture_matured(transport=Transport(),now=now)

if __name__ == '__main__':unittest.main()
