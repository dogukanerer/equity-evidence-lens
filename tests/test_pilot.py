import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "engine"))
import json
import unittest
from unittest.mock import patch
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from safe_rpc import NETWORKS, parse_uint_hex, validate_rpc_request, rpc, check_chain, probe, RPCError
from stock_integrity import ASSETS, check_stock, build_report, dashboard, positive_uint_call

class RPCFixture:
    def __init__(self, network="base-mainnet", supply=123456000000, code="0x6001", fail_methods=None):
        self.cfg = NETWORKS[network]
        self.supply=supply
        self.code=code
        self.fail_methods=set(fail_methods or [])
        self.calls=[]
    def __call__(self, url, payload):
        self.calls.append(payload)
        if url != self.cfg.url:
            raise AssertionError("Unexpected RPC url")
        method = payload["method"]
        if method in self.fail_methods:
            return {"jsonrpc":"2.0","id":1,"error":{"code":-1,"message":"not supported"}}
        result={"eth_chainId": hex(self.cfg.chain_id),"eth_blockNumber":"0x11", "eth_gasPrice":"0x4a817c800", "eth_getCode":self.code}.get(method)
        if method == "eth_call":
            selector=payload["params"][0]["data"]
            result="0x"+format(self.supply if selector=="0x18160ddd" else 6,"064x")
        return {"jsonrpc":"2.0","id":1,"result":result}

class SafeRPCTests(unittest.TestCase):
    def test_networks_distinct(self):
        self.assertEqual(len({n.chain_id for n in NETWORKS.values()}),len(NETWORKS))
    def test_no_state_changing_methods(self):
        for method in ("eth_sendRawTransaction","eth_sendTransaction","personal_sign", "wallet_addEthereumChain"):
            with self.assertRaises(ValueError):validate_rpc_request(method,[])
    def test_wrong_network_disallowed(self):
        with self.assertRaises(ValueError):rpc("localhost", "eth_chainId")
    def test_getcode_input_requires_valid_address(self):
        with self.assertRaises(ValueError):validate_rpc_request("eth_getCode",["0x123","latest"])
    def test_call_input_requires_bounded_data(self):
        with self.assertRaises(ValueError):validate_rpc_request("eth_call",[{"to":ASSETS['AAPLc'], "data":"0x"+"00"*101},"latest"])
    def test_no_extra_call_fields(self):
        with self.assertRaises(ValueError):validate_rpc_request("eth_call",[{"to":ASSETS['AAPLc'], "data":"0x18160ddd", "value":"0xffff"},"latest"])
    def test_chain_valid(self):
        f=RPCFixture()
        self.assertEqual(check_chain("base-mainnet",f),8453)
    def test_chain_mismatch(self):
        cfg=NETWORKS["base-mainnet"]
        def wrong_chain(url,payload):
            self.assertEqual(url,cfg.url)
            return {"jsonrpc":"2.0","id":1,"result":"0x14a34"}
        with self.assertRaises(RPCError):check_chain("base-mainnet",wrong_chain)
    def test_probe(self):
        f=RPCFixture("arc-testnet")
        res=probe("arc-testnet",f)
        self.assertEqual(res['latest_block'],17)
        self.assertEqual(res['fee_symbol'],'USDC')
        self.assertEqual(res["status"],"RPC_CHECK_PASSED_READ_ONLY")
    def test_hex_negative(self):
        with self.assertRaises(RPCError):parse_uint_hex("-3")
    def test_invalid_rpc_envelope(self):
        with self.assertRaises(RPCError):rpc("base-mainnet", "eth_chainId", [], lambda *_:{"result":"0x2105"})
    def test_rpc_error_not_ignored(self):
        with self.assertRaises(RPCError):rpc("base-mainnet", "eth_chainId", [], RPCFixture(fail_methods={"eth_chainId"}))

class StockTests(unittest.TestCase):
    def test_three_allowlisted(self):self.assertEqual(set(ASSETS),{"AAPLc","NVDAc","TSLAc"})
    def test_stock_code_and_supply(self):
        item=check_stock("AAPLc",RPCFixture())
        self.assertEqual(item['status'],"NONZERO_SUPPLY_REPORTED")
        self.assertEqual(item['decimals_reported'],6)
    def test_zero_supply(self):
        item=check_stock("AAPLc",RPCFixture(supply=0))
        self.assertEqual(item['status'],"ZERO_SUPPLY_REPORTED")
    def test_no_code(self):self.assertEqual(check_stock("AAPLc",RPCFixture(code="0x"))['status'],"NO_CODE")
    def test_missing_supply_not_validated(self):
        item=check_stock("AAPLc",RPCFixture(fail_methods={"eth_call"}))
        self.assertEqual(item['status'],"CODE_PRESENT_SUPPLY_UNVERIFIED")
    def test_unknown_ticker_rejected(self):
        with self.assertRaises(ValueError):check_stock("FAKEc",RPCFixture())
    def test_demo_report_labeled(self):
        rep=build_report([{"ticker":"AAPLc","address":ASSETS['AAPLc'],"status":"NOT_VERIFIED"}],demo=True)
        self.assertEqual(rep['mode'],"OFFLINE_DEMO_SYNTHETIC")
    def test_xss_escaped(self):
        rep=build_report([{"ticker":"<script>alert(1)</script>","address":"x","status":"NOT_VERIFIED"}],demo=True)
        rendered=dashboard(rep)
        self.assertNotIn('<script>alert(1)</script>',rendered)
        self.assertIn('&lt;script&gt;', rendered)
    def test_actual_html_contains_caveat(self):
        rep=build_report([{"ticker":"AAPLc","address":ASSETS['AAPLc'],"status":"NONZERO_SUPPLY_REPORTED","total_supply_raw":"100"}],demo=False)
        self.assertIn('READ_ONLY',rep['mode'])
        self.assertIn('haklarını doğrulamaz',dashboard(rep))

if __name__=="__main__":unittest.main()
