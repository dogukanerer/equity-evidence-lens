import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / 'engine'))
spec=importlib.util.spec_from_file_location('b20_native_probe',ROOT / 'scripts' / 'b20_native_probe.py')
b20=importlib.util.module_from_spec(spec)
spec.loader.exec_module(b20)
from stock_integrity import ASSETS

NOW=1794787200  # deterministic Nov 2026 UTC reference
STAMP=NOW-120
BLOCK=55000000
H='0x'+'ab'*32

def uint(i): return '0x'+format(i,'064x')

class Fake:
    def __init__(self):
        self.calls=[]
        self.chain=8453
        self.tip=BLOCK+32
        self.hashes=[H,H]
        self.reads=0
        self.values={name:{
            'total_supply_raw':600000000000,'decimals':8,
            'ui_multiplier':10**18,'new_ui_multiplier':10**18,
            'effective_at':0,'total_supply_ui_raw':600000000000
        } for name in ASSETS}
    def __call__(self,url,payload):
        method=payload['method']; params=payload['params']; self.calls.append((method,params))
        if method=='eth_chainId': val=hex(self.chain)
        elif method=='eth_blockNumber': val=hex(self.tip)
        elif method=='eth_getBlockByNumber':
            index=min(self.reads,len(self.hashes)-1); self.reads+=1
            val={'number':hex(BLOCK),'hash':self.hashes[index],'timestamp':hex(STAMP)}
        elif method=='eth_call':
            ticker=next(k for k,v in ASSETS.items() if v.lower()==params[0]['to'].lower())
            selector=params[0]['data']
            key=next(k for k,v in b20.SELECTORS.items() if v==selector)
            val=uint(self.values[ticker][key])
        else: raise AssertionError('unexpected RPC method '+method)
        return {'jsonrpc':'2.0','id':1,'result':val}

class Tests(unittest.TestCase):
    def test_selectors(self):
        self.assertEqual(b20.SELECTORS,{
            'total_supply_raw':'0x18160ddd','decimals':'0x313ce567',
            'ui_multiplier':'0xa60bf13d','new_ui_multiplier':'0xdc767007',
            'effective_at':'0x97a4064f','total_supply_ui_raw':'0x9bea6429'})
    def test_no_network_fixture(self):
        f=b20.synthetic()
        self.assertEqual(f['mode'],'OFFLINE_SYNTHETIC_NOT_LIVE')
        self.assertEqual(len(f['assets']),3)
    def test_live_read_only(self):
        fake=Fake(); result=b20.collect(transport=fake,now=datetime.fromtimestamp(NOW,timezone.utc))
        self.assertEqual(result['block'],BLOCK)
        self.assertEqual(len(result['assets']),3)
        self.assertEqual({a['ticker'] for a in result['assets']},set(ASSETS))
        self.assertEqual(set(m for m,_ in fake.calls),{'eth_chainId','eth_blockNumber','eth_getBlockByNumber','eth_call'})
        self.assertEqual(result['assets'][0]['ui_multiplier_factor'],'1')
        self.assertEqual(result['assets'][0]['raw_supply_units'],'6000')
    def test_pending_multiplier_true_only_future(self):
        vals={'total_supply_raw':150000000,'decimals':8,'ui_multiplier':10**18,
              'new_ui_multiplier':2*10**18,'effective_at':STAMP+1200,
              'total_supply_ui_raw':150000000}
        self.assertTrue(b20.normalize_asset_values(vals,STAMP)['pending_multiplier_update'])
        self.assertEqual(b20.normalize_asset_values(vals,STAMP)['ui_adjusted_supply_units'],'1.5')
    def test_matured_timestamp_not_pending(self):
        vals={'total_supply_raw':150000000,'decimals':8,'ui_multiplier':2*10**18,
              'new_ui_multiplier':2*10**18,'effective_at':STAMP-100,
              'total_supply_ui_raw':300000000}
        self.assertFalse(b20.normalize_asset_values(vals,STAMP)['pending_multiplier_update'])
    def test_bad_matured_schedule(self):
        vals={'total_supply_raw':150000000,'decimals':8,'ui_multiplier':10**18,
              'new_ui_multiplier':2*10**18,'effective_at':STAMP-100,
              'total_supply_ui_raw':150000000}
        with self.assertRaises(b20.ProbeError):b20.normalize_asset_values(vals,STAMP)
    def test_ui_math_integrity(self):
        vals={'total_supply_raw':150000000,'decimals':8,'ui_multiplier':2*10**18,
              'new_ui_multiplier':2*10**18,'effective_at':0,
              'total_supply_ui_raw':150000000}
        with self.assertRaisesRegex(b20.ProbeError,'UI_SUPPLY_IDENTITY_FAILED'):
            b20.normalize_asset_values(vals,STAMP)
    def test_bad_abi_word(self):
        for val in ['0x123','0x'+'00'*33,0,None,'0x'+'zz'*32]:
            with self.assertRaises(b20.ProbeError):b20.decode_word(val,'test')
    def test_wrong_chain_fails(self):
        fake=Fake();fake.chain=1
        with self.assertRaises(Exception):b20.collect(transport=fake,now=datetime.fromtimestamp(NOW,timezone.utc))
        self.assertEqual(len(fake.calls),1)
    def test_block_hash_changed_rejected(self):
        fake=Fake();fake.hashes=[H,'0x'+'cd'*32]
        with self.assertRaisesRegex(b20.ProbeError,'BLOCK_CHANGED'):
            b20.collect(transport=fake,now=datetime.fromtimestamp(NOW,timezone.utc))
    def test_stale_block_fails(self):
        fake=Fake()
        with self.assertRaisesRegex(b20.ProbeError,'STALE_OR_FUTURE_BLOCK'):
            b20.collect(transport=fake,now=datetime.fromtimestamp(NOW+7200,timezone.utc))
    def test_bad_lag_fails_before_network(self):
        fake=Fake()
        for lag in (-1,0,1,513,True):
            with self.assertRaises(b20.ProbeError):b20.collect(transport=fake,lag_blocks=lag)
        self.assertEqual(fake.calls,[])
    def test_bad_decimals(self):
        fake=Fake(); fake.values['AAPLc']['decimals']=30
        with self.assertRaisesRegex(b20.ProbeError,'UNSUPPORTED_B20_DECIMALS'):
            b20.collect(transport=fake,now=datetime.fromtimestamp(NOW,timezone.utc))
    def test_no_input_modification(self):
        fake=Fake(); original=copy.deepcopy(fake.values)
        b20.collect(transport=fake,now=datetime.fromtimestamp(NOW,timezone.utc))
        self.assertEqual(fake.values,original)
    def test_no_file_creation_by_live_method(self):
        # collect returns a dict, no file-writing behavior exists in the probe.
        result=b20.collect(transport=Fake(),now=datetime.fromtimestamp(NOW,timezone.utc))
        self.assertNotIn('output_path',result)
    def test_bad_ui_multiplier(self):
        vals={'total_supply_raw':1,'decimals':8,'ui_multiplier':0,
              'new_ui_multiplier':1,'effective_at':0,'total_supply_ui_raw':0}
        with self.assertRaises(b20.ProbeError):b20.normalize_asset_values(vals,STAMP)

if __name__=='__main__':unittest.main()
