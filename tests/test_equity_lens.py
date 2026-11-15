import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "engine"))
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safe_rpc import NETWORKS, RPCError, rpc, validate_rpc_request
from equity_lens import (ASSETS, EvidenceError, pinned_block, read_asset, capture,
                         compare, make_demo, render_snapshot, render_comparison,
                         read_snapshot, save_json, main)

class EquityFixture:
    def __init__(self, height=100, code='0x6001', supply=1000000000, decimals=6,
                 fail_method=None, header_number=None, chain_id=8453, timestamp=1794744000):
        self.height=height
        self.code=code
        self.supply=supply
        self.decimals=decimals
        self.fail_method=fail_method
        self.header_number=header_number
        self.chain_id=chain_id
        self.timestamp=timestamp
        self.calls=[]
    def __call__(self,url,payload):
        assert url==NETWORKS['base-mainnet'].url
        self.calls.append(payload)
        m=payload['method']
        if self.fail_method==m:
            return {'jsonrpc':'2.0','id':1,'error':{'code':-1,'message':'synthetic error'}}
        if m=='eth_chainId':value=hex(self.chain_id)
        elif m=='eth_blockNumber':value=hex(self.height)
        elif m=='eth_getBlockByNumber':
            value={'number':hex(self.header_number if self.header_number is not None else self.height),
                   'hash':'0x'+'a'*64,'timestamp':hex(self.timestamp)}
        elif m=='eth_getCode':value=self.code
        elif m=='eth_call':
            selector=payload['params'][0]['data']
            value='0x'+format(self.supply if selector=='0x18160ddd' else self.decimals,'064x')
        else:raise AssertionError(m)
        return {'jsonrpc':'2.0','id':1,'result':value}

class EquityLensTests(unittest.TestCase):
    def test_old_rpc_rejects_send(self):
        with self.assertRaises(ValueError): validate_rpc_request('eth_sendTransaction', [])
    def test_block_tag_rejects_non_string(self):
        with self.assertRaises(ValueError): validate_rpc_request('eth_getCode',[ASSETS['AAPLc'],123])
    def test_block_tag_rejects_injection(self):
        with self.assertRaises(ValueError): validate_rpc_request('eth_getCode',[ASSETS['AAPLc'],'latest;eth_sendRawTransaction'])
    def test_get_block_no_full_transactions(self):
        with self.assertRaises(ValueError): validate_rpc_request('eth_getBlockByNumber',['0x64',True])
    def test_block_hash_check_and_pinning(self):
        f=EquityFixture()
        block=pinned_block(f)
        self.assertEqual(block['number'],100)
        self.assertEqual(block['block_tag'],'0x64')
        self.assertEqual(len(block['hash']),66)
        snap=capture(f)
        self.assertEqual(len(snap['assets']),3)
        calls=[x for x in f.calls if x['method'] in {'eth_call','eth_getCode'}]
        self.assertTrue(all(x['params'][1]=='0x64' for x in calls))
        self.assertEqual(set(x['method'] for x in f.calls), {'eth_chainId','eth_blockNumber','eth_getBlockByNumber','eth_getCode','eth_call'})
    def test_wrong_chain_rejected(self):
        with self.assertRaises(RPCError):pinned_block(EquityFixture(chain_id=1))
    def test_mismatched_header_rejected(self):
        with self.assertRaises(EvidenceError):pinned_block(EquityFixture(header_number=99))
    def test_future_time_rejected(self):
        with self.assertRaises(EvidenceError):pinned_block(EquityFixture(timestamp=2000000000))
    def test_asset_returns_sha256_and_units(self):
        f=EquityFixture(supply=12000000)
        v=read_asset('AAPLc','0x64',f)
        self.assertEqual(v['status'],'READ_OK')
        self.assertEqual(v['token_units_reported'],'12')
        self.assertEqual(len(v['code_sha256']),64)
    def test_partial_asset_failure_not_success(self):
        v=read_asset('AAPLc','0x64',EquityFixture(fail_method='eth_call'))
        self.assertEqual(v['status'],'NOT_VERIFIED')
    def test_missing_code(self):
        v=read_asset('AAPLc','0x64',EquityFixture(code='0x'))
        self.assertEqual(v['status'],'NO_CODE')
    def test_non_allowlisted_asset(self):
        with self.assertRaises(EvidenceError):read_asset('FAKEc','0x64',EquityFixture())
    def test_demo_changed_two(self):
        a,b=make_demo()
        r=compare(a,b)
        self.assertEqual([v['status'] for v in r['results']],['CHANGE_REVIEW','NO_CHANGE_AT_SAMPLED_BLOCKS','CHANGE_REVIEW'])
        self.assertEqual(r['results'][0]['raw_supply_pct_change'],'20.000000')
        self.assertEqual(r['results'][2]['changes'],['RUNTIME_CODE_SHA256_CHANGED'])
    def test_same_block_rejected(self):
        a,b=make_demo()
        b['block']['number']=a['block']['number']
        with self.assertRaises(EvidenceError):compare(a,b)
    def test_synthetic_live_mixed_rejected(self):
        a,b=make_demo()
        b['mode']='LIVE_BASE_MAINNET_READ_ONLY'
        with self.assertRaises(EvidenceError):compare(a,b)
    def test_tampered_asset_address_rejected(self):
        a,b=make_demo()
        b['assets'][0]['address']=ASSETS['TSLAc']
        with self.assertRaises(EvidenceError):compare(a,b)
    def test_incomplete_asset_set_rejected(self):
        a,b=make_demo()
        b['assets'].pop()
        with self.assertRaises(EvidenceError):compare(a,b)
    def test_report_escapes_user_supplied_notes(self):
        a,b=make_demo()
        a['assets'][0]['ticker']='AAPLc' # invariant allowlist
        a['assets'][0]['token_units_reported']='<img src=x onerror=alert(1)>'
        out=render_snapshot(a)
        self.assertNotIn('<img src=x onerror=alert(1)>',out)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;',out)
    def test_compare_html_warning_and_demo(self):
        a,b=make_demo()
        rendered=render_comparison(compare(a,b))
        self.assertIn('OFFLINE_DEMO_SYNTHETIC',rendered)
        self.assertIn('yatırım tavsiyesi',rendered)
        self.assertIn('CHANGE_REVIEW',rendered)
    def test_snapshot_save_load_and_limit(self):
        a,b=make_demo()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'snap.json'
            save_json(p,a,exclusive=True)
            self.assertEqual(read_snapshot(p)['block']['number'],100)
            with self.assertRaises(FileExistsError):save_json(p,a,exclusive=True)
            p.write_text(' '*200001)
            with self.assertRaises(EvidenceError):read_snapshot(p)
    def test_compare_missing_supply_review_not_positive(self):
        a,b=make_demo()
        b['assets'][0]['status']='NOT_VERIFIED'
        result=compare(a,b)
        self.assertEqual(result['results'][0]['status'],'INSUFFICIENT_EVIDENCE')
    def test_0_previous_supply_percent_unknown(self):
        a,b=make_demo()
        a['assets'][0]['total_supply_raw']='0'
        result=compare(a,b)
        self.assertIsNone(result['results'][0]['raw_supply_pct_change'])
        self.assertEqual(result['results'][0]['status'],'CHANGE_REVIEW')
    def test_unchanged_live_two_snapshots(self):
        first=capture(EquityFixture(height=100))
        next_=capture(EquityFixture(height=101))
        self.assertTrue(all(r['status']=='NO_CHANGE_AT_SAMPLED_BLOCKS' for r in compare(first,next_)['results']))
    def test_demo_command_no_network(self):
        self.assertEqual(main(['demo']),0)

if __name__=='__main__':unittest.main()
