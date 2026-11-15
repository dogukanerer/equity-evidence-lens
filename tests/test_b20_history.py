import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'engine'))
from scripts import b20_history as bh
from scripts.b20_native_probe import normalize_asset_values
from stock_integrity import ASSETS

TS=1794787200
HASH=lambda n:'0x'+f'{n:064x}'

def core_item(n):
    return {'block':{'number':n,'hash':HASH(n),'block_timestamp_utc':datetime.fromtimestamp(TS+n-101,timezone.utc).isoformat()},
            'assets':[{'ticker':name,'total_supply_raw':str({'AAPLc':600017259753,'NVDAc':4925560410884,'TSLAc':643288183743}[name]),'decimals_reported':8}
                      for name in ASSETS]}

def core_fixture(n=103):
    return [core_item(i) for i in range(101,n+1)]

def record(block,*,multiplier=None,pending=False):
    timestamp=int(datetime.fromisoformat(block['block_timestamp_utc']).timestamp())
    assets=[]
    for ticker,addr in ASSETS.items():
        m=multiplier if ticker=='NVDAc' and multiplier is not None else 10**18
        supply={'AAPLc':600017259753,'NVDAc':4925560410884,'TSLAc':643288183743}[ticker]
        r={'total_supply_raw':supply,'decimals':8,'ui_multiplier':m,
           'new_ui_multiplier':(m+1) if pending else m,
           'effective_at':(timestamp+3600) if pending else 0,
           'total_supply_ui_raw':supply*m//10**18}
        assets.append({'ticker':ticker,'address':addr,**normalize_asset_values(r,timestamp)})
    return {'schema_version':bh.SCHEMA,'chain_id':8453,'mode':'LIVE_BASE_MAINNET_READ_ONLY',
            'block':{'number':block['number'],'hash':block['hash'],'timestamp_utc':block['block_timestamp_utc']},
            'assets':assets,'scope':'Read-only native B20 Asset calls; raw supply and multiplier-adjusted UI supply are different concepts.'}

class B20HistoryTests(unittest.TestCase):
    def setUp(self):
        self.d=TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root=Path(self.d.name)
        (self.root/'index.html').write_text('<html><main><p>Current</p></main></html>',encoding='utf-8')
        (self.root/'data').mkdir()
        self.core=core_fixture()
        self.collector=lambda block:record(block)
    def _process(self,**kwargs):
        with patch.object(bh,'history_from_site',return_value=self.core):
            return bh.process(self.root,collector=self.collector,**kwargs)
    def test_check_only_before_any_state(self):
        d=self._process(check_only=True)
        self.assertEqual(d['b20_observations'],0)
        self.assertFalse((self.root/'data/b20_latest.json').exists())
    def test_pinned_rpc_reads_no_latest_or_wallet_and_checks_hash(self):
        current=self.core[-1]['block']
        stamp=int(datetime.fromisoformat(current['block_timestamp_utc']).timestamp())
        called=[]
        def network(_url,payload):
            method=payload['method'];params=payload['params'];called.append((method,params))
            if method=='eth_chainId': val=hex(8453)
            elif method=='eth_getBlockByNumber':
                val={'number':hex(current['number']),'hash':current['hash'],'timestamp':hex(stamp)}
            elif method=='eth_call':
                ticker=next(x for x,a in ASSETS.items() if a.lower()==params[0]['to'].lower())
                selector=params[0]['data']
                field=next(k for k,v in bh.SELECTORS.items() if v==selector)
                supply={'AAPLc':600017259753,'NVDAc':4925560410884,'TSLAc':643288183743}[ticker]
                r={'total_supply_raw':supply,'decimals':8,'ui_multiplier':10**18,
                   'new_ui_multiplier':10**18,'effective_at':0,'total_supply_ui_raw':supply}
                val='0x'+format(r[field],'064x')
            else: raise AssertionError('UNEXPECTED_RPC_METHOD:'+method)
            return {'jsonrpc':'2.0','id':1,'result':val}
        got=bh.collect_pinned(current,transport=network)
        self.assertEqual(got['block']['number'],current['number'])
        self.assertEqual(set(x for x,_ in called),{'eth_chainId','eth_getBlockByNumber','eth_call'})
        self.assertTrue(all(args[1]==hex(current['number']) for m,args in called if m=='eth_call'))
        self.assertNotIn('eth_blockNumber',[m for m,_ in called])
    def test_pinned_rpc_reorg_refused(self):
        current=self.core[-1]['block']
        stamp=int(datetime.fromisoformat(current['block_timestamp_utc']).timestamp())
        def network(_url,payload):
            method=payload['method'];params=payload['params']
            if method=='eth_chainId':v=hex(8453)
            elif method=='eth_getBlockByNumber':v={'number':hex(current['number']),'hash':HASH(999),'timestamp':hex(stamp)}
            else: raise AssertionError('Should fail before calls')
            return {'jsonrpc':'2.0','id':1,'result':v}
        with self.assertRaisesRegex(Exception,'IDENTITY_MISMATCH'):
            bh.collect_pinned(current,transport=network)
    def test_initial_capture_is_baseline_not_event(self):
        d=self._process()
        self.assertTrue(d['written'])
        self.assertEqual(d['b20_observations'],1)
        body=(self.root/'index.html').read_text()
        self.assertIn('İlk B20 gözlemi',body)
        self.assertEqual(body.count('id="b20-native-evidence-v1"'),1)
    def test_repeated_same_block_is_no_write(self):
        self._process()
        old=(self.root/'data/b20_history.json').read_bytes()
        self.assertFalse(self._process()['written'])
        self.assertEqual((self.root/'data/b20_history.json').read_bytes(),old)
    def test_second_block_detects_multiplier_change_only(self):
        self._process()
        self.core=core_fixture(104)
        self.collector=lambda block:record(block,multiplier=1000537939576369481)
        d=self._process()
        self.assertEqual(d['b20_observations'],2)
        self.assertEqual(d['ui_supply_difference_assets'],['NVDAc'])
        body=(self.root/'index.html').read_text()
        self.assertIn('ui_multiplier_wad',body)
        self.assertEqual(body.count('id="b20-native-evidence-v1"'),1)
    def test_manifest_binds_output_bytes(self):
        self._process()
        manifest=json.loads((self.root/'data/b20_manifest.json').read_text())
        for f,d in manifest['files_sha256'].items():
            self.assertEqual(hashlib.sha256((self.root/'data'/f).read_bytes()).hexdigest(),d)
    def test_corrupt_manifest_prevents_next_capture_and_write(self):
        self._process()
        (self.root/'data/b20_latest.json').write_text('{}')
        self.core=core_fixture(104)
        with self.assertRaisesRegex(Exception,'INTEGRITY'):
            self._process()
        self.assertNotIn('104', (self.root/'data/b20_history.json').read_text())
    def test_partial_records_are_rejected(self):
        (self.root/'data/b20_latest.json').write_text('{}')
        with self.assertRaisesRegex(Exception,'PARTIAL'):
            self._process(check_only=True)
    def test_wrong_block_hash_refused(self):
        self.collector=lambda block:{**record(block),'block':{**record(block)['block'],'hash':HASH(999)}}
        with self.assertRaisesRegex(Exception,'BLOCK_MISMATCH'):
            self._process()
        self.assertFalse((self.root/'data/b20_history.json').exists())
    def test_missing_token_refused(self):
        self.collector=lambda block:{**record(block),'assets':record(block)['assets'][:-1]}
        with self.assertRaisesRegex(Exception,'ASSETS'):
            self._process()
    def test_spoofed_token_address_refused(self):
        def bad(block):
            r=record(block);r['assets'][0]['address']='0x'+'ab'*20;return r
        self.collector=bad
        with self.assertRaisesRegex(Exception,'ADDRESS_MISMATCH'):
            self._process()
    def test_core_supply_mismatch_refused(self):
        def bad(block):
            r=record(block);r['assets'][1]['total_supply_raw']='4925560410885'
            r['assets'][1]['total_supply_ui_raw']='4925560410885'
            r['assets'][1]['raw_supply_units']='49255.60410885'
            r['assets'][1]['ui_adjusted_supply_units']='49255.60410885'
            return r
        self.collector=bad
        with self.assertRaisesRegex(Exception,'CORE_SUPPLY_OR_DECIMALS_DISAGREE'):
            self._process()
    def test_false_adjusted_supply_refused(self):
        def bad(block):
            r=record(block);r['assets'][1]['total_supply_ui_raw']='999';return r
        self.collector=bad
        with self.assertRaisesRegex(Exception,'INVALID_ASSET_MATH'):
            self._process()
    def test_tampered_boolean_pending_refused(self):
        def bad(block):
            r=record(block);r['assets'][1]['pending_multiplier_update']=True;return r
        self.collector=bad
        with self.assertRaisesRegex(Exception,'TAMPERED'):
            self._process()
    def test_unsafe_html_baseline_rejected(self):
        (self.root/'index.html').write_text('<h1>Lost main</h1>')
        with self.assertRaisesRegex(Exception,'MAIN_MISSING'):
            self._process()
    def test_existing_section_replacement(self):
        self._process()
        self.core=core_fixture(104)
        self.collector=lambda block:record(block)
        self._process()
        self.assertEqual((self.root/'index.html').read_text().count(bh.BEGIN),1)
    def test_duplicate_marker_rejected(self):
        x=bh.BEGIN+'something'+bh.END
        with self.assertRaisesRegex(Exception,'MARKER_CORRUPT'):
            bh.html_with_section('<main>'+x+x+'</main>','hello')
    def test_pending_multiplier_explained(self):
        r=record(self.core[-1]['block'],pending=True)
        text=bh.render_section([r])
        self.assertIn('İleri tarihli multiplier',text)
    def test_no_pending_not_claimed_forever(self):
        r=record(self.core[-1]['block'])
        text=bh.render_section([r])
        self.assertIn('Gelecekte değişmeyeceği',text)
    def test_unverified_core_does_not_call_collector(self):
        with patch.object(bh,'history_from_site',side_effect=ValueError('bad core')):
            with self.assertRaises(ValueError):bh.process(self.root,collector=lambda _:self.fail('Should not collect'))
    def test_public_scope_no_wallet(self):
        self._process()
        text=(self.root/'index.html').read_text()
        self.assertIn('kriptografik durum ispatı değildir',text)

class ApplyPatchTests(unittest.TestCase):
    def test_workflow_is_idempotent_and_ordered(self):
        import apply_b20_history_patch as ap
        reference=(ROOT/'.github/workflows/equity-evidence-pages.yml').read_text(encoding='utf-8')
        out=ap.transform(reference)
        self.assertEqual(ap.transform(out),out)
        self.assertLess(out.index(ap.BUILD_EVENT),out.index(ap.BUILD_B20))
        self.assertLess(out.index(ap.BUILD_B20),out.index('git add index.html'))
    def test_wrong_workflow_rejected(self):
        import apply_b20_history_patch as ap
        with self.assertRaisesRegex(ValueError,'WORKFLOW'):
            ap.transform('name: some different workflow')

if __name__=='__main__': unittest.main()
