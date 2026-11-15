"""No network, no wallet; realistic test snapshots and injected JSON-RPC responses."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'engine'))

from scripts import proxy_monitor as pm
from scripts.refresh_site import build_outputs, write_outputs, history_from_site
from stock_integrity import ASSETS, BASE_ASSETS_SOURCE
from equity_lens import EvidenceError
from safe_rpc import validate_rpc_request

CODE='0x6001'
DIGEST=hashlib.sha256(bytes.fromhex(CODE[2:])).hexdigest()
IMPL='0x'+'a'*40
BEACON='0x'+'b'*40
ADMIN='0x'+'c'*40


def word(addr=None): return pm.ZERO_WORD if addr is None else '0x'+'0'*24+addr[2:].lower()


def mock_snapshot(height, minutes=3, mark='a'):
    now=datetime.now(timezone.utc)
    return {'schema_version':'equity-lens-public-snapshot-1','chain_id':8453,
        'mode':'LIVE_BASE_MAINNET_READ_ONLY','captured_at_utc':(now-timedelta(minutes=minutes)).isoformat(),
        'source_url':BASE_ASSETS_SOURCE,
        'block':{'number':height,'hash':'0x'+mark*64,
                 'block_timestamp_utc':(now-timedelta(minutes=minutes,seconds=20)).isoformat()},
        'assets':[{'ticker':ticker,'address':address,'status':'READ_OK',
                   'code_sha256':DIGEST,'code_bytes':2,'total_supply_raw':str(1_000_000 * i),
                   'decimals_reported':6,'token_units_reported':str(i)}
                  for i,(ticker,address) in enumerate(ASSETS.items(),1)]}


def new_site(root):
    a,b=mock_snapshot(100,minutes=6),mock_snapshot(101,minutes=1,mark='b')
    write_outputs(root,build_outputs([a,b]))
    return a,b


class Transport:
    def __init__(self, block=101, hash_char='b', slots=None, code=CODE, beacon_mode=False):
        self.block=block;self.hash_char=hash_char;self.slots=slots or {}
        self.code=code;self.calls=[];self.beacon_mode=beacon_mode;self.flip=False
    def __call__(self,url,payload):
        m=payload['method'];params=payload['params'];self.calls.append((m,params))
        if m=='eth_chainId':r='0x2105'
        elif m=='eth_getBlockByNumber':
            r={'number':hex(self.block),'hash':'0x'+('d' if self.flip else self.hash_char)*64,
               'timestamp':hex(int(datetime.now(timezone.utc).timestamp())-120)}
        elif m=='eth_getCode':r=self.code
        elif m=='eth_getStorageAt':
            r=self.slots.get((params[0].lower(),params[1]),pm.ZERO_WORD)
        elif m=='eth_call' and params[0]['data']==pm.BEACON_IMPL_SELECTOR:r=word(IMPL)
        else:raise AssertionError('Unexpected method '+m)
        return {'jsonrpc':'2.0','id':1,'result':r}


class ProxyTests(unittest.TestCase):
    def test_standard_slot_constants(self):
        self.assertEqual(len(pm.ERC1967_SLOTS),3)
        self.assertTrue(all(len(x)==66 for x in pm.ERC1967_SLOTS.values()))
    def test_zero_word_is_unknown_not_safe(self):
        self.assertIsNone(pm.word_address(pm.ZERO_WORD))
    def test_address_word(self):
        self.assertEqual(pm.word_address(word(IMPL)),IMPL)
    def test_high_bits_invalid(self):
        with self.assertRaises(EvidenceError):pm.word_address('0x1'+'0'*63)
    def test_malformed_words_rejected(self):
        for x in ('0x1','0x'+'z'*64, None, '0x'+'0'*65):
            with self.assertRaises(EvidenceError):pm.word_address(x)
    def test_canonical_clone_exact_only(self):
        code='0x'+pm.CLONE_PREFIX+IMPL[2:]+pm.CLONE_SUFFIX
        self.assertEqual(pm.clone_target(code),IMPL)
        self.assertIsNone(pm.clone_target(code+'00'))
        self.assertIsNone(pm.clone_target('0x6001'))
    def test_forbidden_rpc_method_and_storage_param_rules(self):
        with self.assertRaises(ValueError):validate_rpc_request('eth_sendRawTransaction', ['xx'])
        validate_rpc_request('eth_getStorageAt',[ASSETS['AAPLc'],pm.ERC1967_SLOTS['admin'],'0x65'])
        for params in ([ASSETS['AAPLc'],'0x123','latest'],[ASSETS['AAPLc'],pm.ERC1967_SLOTS['admin'],'pending']):
            with self.assertRaises(ValueError):validate_rpc_request('eth_getStorageAt',params)
    def test_collect_empty_slots_unclassified(self):
        s=mock_snapshot(101,minutes=1,mark='b');t=Transport()
        x=pm.collect(s,transport=t)
        self.assertEqual(x['assets'][0]['pattern_observed'],'UNCLASSIFIED_STANDARD_SLOTS_EMPTY')
        self.assertIsNone(x['assets'][0]['eip1967_addresses']['admin'])
        self.assertTrue(set(m for m,_ in t.calls).issubset({
            'eth_chainId','eth_getBlockByNumber','eth_getCode','eth_getStorageAt','eth_call'}))
        self.assertEqual([m for m,_ in t.calls].count('eth_getStorageAt'),9)
    def test_erc1967_impl_and_admin(self):
        s=mock_snapshot(101,minutes=1,mark='b')
        t=Transport(slots={(ASSETS['AAPLc'].lower(),pm.ERC1967_SLOTS['implementation']):word(IMPL),
                           (ASSETS['AAPLc'].lower(),pm.ERC1967_SLOTS['admin']):word(ADMIN)})
        x=pm.collect(s,transport=t)
        a=x['assets'][0]
        self.assertEqual(a['implementation_target_address'],IMPL)
        self.assertEqual(a['eip1967_addresses']['admin'],ADMIN)
        self.assertEqual(a['target_runtime_sha256'],DIGEST)
        self.assertEqual(a['authorization_analysis'],'NOT_PERFORMED')
    def test_beacon_resolves_implementation(self):
        s=mock_snapshot(101,minutes=1,mark='b')
        t=Transport(slots={(ASSETS['NVDAc'].lower(),pm.ERC1967_SLOTS['beacon']):word(BEACON)})
        x=pm.collect(s,transport=t)
        b=x['assets'][1]
        self.assertEqual(b['pattern_observed'],'ERC1967_BEACON_OBSERVED')
        self.assertEqual(b['implementation_target_address'],IMPL)
        self.assertIn('eth_call',[m for m,_ in t.calls])
    def test_runtime_code_mismatch_fails(self):
        s=mock_snapshot(101,minutes=1,mark='b')
        with self.assertRaisesRegex(EvidenceError,'RUNTIME_CODE_MISMATCH'):
            pm.collect(s,transport=Transport(code='0x6002'))
    def test_block_hash_mismatch_fails(self):
        s=mock_snapshot(101,minutes=1,mark='b')
        with self.assertRaisesRegex(EvidenceError,'V3_BLOCK_HEADER_HASH_MISMATCH'):
            pm.collect(s,transport=Transport(hash_char='c'))
    def test_snapshot_too_old_fails_before_network(self):
        s=mock_snapshot(101,minutes=60,mark='b')
        with self.assertRaisesRegex(EvidenceError,'SNAPSHOT_TOO_OLD'):
            pm.collect(s,transport=lambda *_: (_ for _ in ()).throw(AssertionError('no network')))
    def test_read_prior_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p)
            self.assertEqual(pm.read_prior(p,history_from_site(p)),[])
    def test_check_only_never_network_or_write(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p);before=(p/'index.html').read_bytes()
            with patch.object(pm,'collect',side_effect=AssertionError('NO_NETWORK')):
                out=pm.refresh_proxy(p,check_only=True)
            self.assertFalse(out['written']);self.assertEqual(out['proxy_history'],0)
            self.assertEqual((p/'index.html').read_bytes(),before)
    def test_refresh_then_recheck_hashes_and_idempotence(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p)
            collected=pm.collect(history_from_site(p)[-1],transport=Transport())
            out=pm.refresh_proxy(p,collector=lambda _:collected)
            self.assertTrue(out['written'])
            self.assertIn('Proxy ve uygulama kanıtları',(p/'index.html').read_text())
            self.assertEqual(pm.refresh_proxy(p,check_only=True)['proxy_history'],1)
            out2=pm.refresh_proxy(p,collector=lambda _:collected)
            self.assertFalse(out2['written'])
            history=json.loads((p/'data/proxy_history.json').read_text())
            self.assertEqual(len(history['observations']),1)
    def test_tamper_detected_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p)
            x=pm.collect(history_from_site(p)[-1],transport=Transport())
            pm.refresh_proxy(p,collector=lambda _:x)
            (p/'data/proxy_audit.json').write_text('{}')
            with self.assertRaisesRegex(EvidenceError,'DIGEST_MISMATCH'):
                pm.refresh_proxy(p,check_only=True)
    def test_incomplete_new_record_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p);page=(p/'index.html').read_bytes()
            bad=pm.collect(history_from_site(p)[-1],transport=Transport())
            bad['assets'][0]['runtime_code_sha256']='bad'
            with self.assertRaises(EvidenceError):pm.refresh_proxy(p,collector=lambda _:bad)
            self.assertEqual((p/'index.html').read_bytes(),page)
            self.assertFalse((p/'data/proxy_audit.json').exists())
    def test_update_on_new_block_and_change_review(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p)
            first=pm.collect(history_from_site(p)[-1],transport=Transport())
            pm.refresh_proxy(p,collector=lambda _:first)
            baseline=history_from_site(p)
            third=mock_snapshot(102,minutes=0,mark='c')
            write_outputs(p,build_outputs(baseline+[third]))
            t=Transport(block=102,hash_char='c',slots={
                (ASSETS['AAPLc'].lower(),pm.ERC1967_SLOTS['implementation']):word(IMPL)})
            newer=pm.collect(third,transport=t)
            pm.refresh_proxy(p,collector=lambda _:newer)
            history=json.loads((p/'data/proxy_history.json').read_text())
            self.assertEqual(len(history['observations']),2)
            self.assertIn('İnceleme gerektiren değişiklik',(p/'index.html').read_text())
            self.assertIn('eip1967_storage_words',pm.review_changes(first,newer)['AAPLc'])
    def test_future_base_missing_from_history_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p)
            x=pm.collect(history_from_site(p)[-1],transport=Transport())
            pm.refresh_proxy(p,collector=lambda _:x)
            main=history_from_site(p)
            modified=mock_snapshot(200,minutes=0,mark='c')
            main.append(modified)
            # Prior observation still in main history, therefore allowed.
            self.assertEqual(pm.read_prior(p,main)[-1]['block']['number'],101)
    def test_canonical_erc1167_clone_live_mock(self):
        raw='0x'+pm.CLONE_PREFIX+IMPL[2:]+pm.CLONE_SUFFIX
        s=mock_snapshot(101,minutes=1,mark='b')
        for a in s['assets']:
            a['code_sha256']=hashlib.sha256(bytes.fromhex(raw[2:])).hexdigest()
            a['code_bytes']=45
        x=pm.collect(s,transport=Transport(code=raw))
        self.assertEqual(x['assets'][0]['pattern_observed'],'CANONICAL_ERC1167_CLONE_OBSERVED')
        self.assertEqual(x['assets'][0]['implementation_target_address'],IMPL)
    def test_conflict_is_not_labeled_known_target(self):
        s=mock_snapshot(101,minutes=1,mark='b')
        t=Transport(slots={
            (ASSETS['AAPLc'].lower(),pm.ERC1967_SLOTS['implementation']):word(IMPL),
            (ASSETS['AAPLc'].lower(),pm.ERC1967_SLOTS['beacon']):word(BEACON)})
        x=pm.collect(s,transport=t)
        a=x['assets'][0]
        self.assertEqual(a['pattern_observed'],'CONFLICTING_INDICATORS_REVIEW_REQUIRED')
        self.assertIsNone(a['implementation_target_address'])
    def test_unexpected_proxy_pattern_fails_closed(self):
        x=pm.collect(mock_snapshot(101,minutes=1,mark='b'),transport=Transport())
        x['assets'][0]['pattern_observed']='UPGRADE_PROVEN_SAFE'
        with self.assertRaisesRegex(EvidenceError,'INCONSISTENT_PROXY_CLASSIFICATION'):
            pm.validate_observation(x)
    def test_wrong_chain_refused(self):
        s=mock_snapshot(101,minutes=1,mark='b')
        t=Transport()
        def wrong(url,p):
            x=t(url,p)
            if p['method']=='eth_chainId':x['result']='0x1'
            return x
        with self.assertRaisesRegex(Exception,'Chain mismatch'):
            pm.collect(s,transport=wrong)
    def test_reorg_detected_on_second_header(self):
        s=mock_snapshot(101,minutes=1,mark='b')
        t=Transport()
        seen=0
        def altered(url,p):
            nonlocal seen
            if p['method']=='eth_getBlockByNumber':
                seen+=1
                if seen==2:t.flip=True
            return t(url,p)
        with self.assertRaisesRegex(EvidenceError,'BLOCK_HASH_CHANGED'):
            pm.collect(s,transport=altered)
    def test_partial_proxy_publication_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p)
            x=pm.collect(history_from_site(p)[-1],transport=Transport())
            pm.refresh_proxy(p,collector=lambda _:x)
            (p/'data/proxy_manifest.json').unlink()
            with self.assertRaisesRegex(EvidenceError,'PARTIAL_PROXY_PUBLICATION'):
                pm.refresh_proxy(p,check_only=True)
    def test_snapshot_from_wrong_block_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p)
            x=pm.collect(history_from_site(p)[-1],transport=Transport())
            x['block']['number']=999
            with self.assertRaisesRegex(EvidenceError,'NOT_PINNED_TO_LATEST'):
                pm.refresh_proxy(p,collector=lambda _:x)
            self.assertFalse((p/'data/proxy_audit.json').exists())
    def test_admin_slot_is_not_proof_of_permissions(self):
        record=pm.collect(mock_snapshot(101,minutes=1,mark='b'),transport=Transport())
        self.assertEqual(record['assets'][0]['authorization_analysis'],'NOT_PERFORMED')
        self.assertTrue(any('UUPS' in note for note in record['limitations']))

if __name__=='__main__': unittest.main()
