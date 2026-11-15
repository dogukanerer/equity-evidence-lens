#!/usr/bin/env python3
"""Read-only allowlisted EVM JSON-RPC checks. Never signs or sends transactions."""
import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

@dataclass(frozen=True)
class Network:
    chain_id: int
    url: str
    fee_symbol: str
    phase: str

NETWORKS = {
    "base-sepolia": Network(84532, "https://sepolia.base.org", "ETH", "TESTNET"),
    "arc-testnet": Network(5042002, "https://rpc.testnet.arc.io", "USDC", "TESTNET"),
    "hyperevm-testnet": Network(998, "https://rpc.hyperliquid-testnet.xyz/evm", "HYPE", "TESTNET"),
    "base-mainnet": Network(8453, "https://mainnet.base.org", "ETH", "MAINNET_READ_ONLY"),
    "arc-mainnet": Network(5042, "https://rpc.mainnet.arc.io", "USDC", "MAINNET_READ_ONLY"),
}
ALLOWED_METHODS = frozenset({"eth_chainId", "eth_blockNumber", "eth_gasPrice", "eth_getCode", "eth_call", "eth_getBlockByNumber", "eth_getStorageAt"})
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
HEX_RE = re.compile(r"0x(?:[0-9a-fA-F]{2})*\Z")
MAX_RESPONSE_BYTES = 128_000
BLOCK_TAG_RE = re.compile(r"(?:latest|0x[0-9a-fA-F]{1,16})\Z")

class RPCError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RPCError("Redirect denied for allowlisted RPC endpoint")


def validate_rpc_request(method, params):
    if method not in ALLOWED_METHODS:
        raise ValueError(f"Forbidden JSON-RPC method: {method}")
    if method in ("eth_chainId", "eth_blockNumber", "eth_gasPrice"):
        if params != []:
            raise ValueError("No parameters expected")
    elif method == "eth_getCode":
        if not (isinstance(params, list) and len(params) == 2 and isinstance(params[0], str) and ADDRESS_RE.fullmatch(params[0]) and isinstance(params[1], str) and BLOCK_TAG_RE.fullmatch(params[1])):
            raise ValueError("getCode requires a valid address and safe block tag")
    elif method == "eth_call":
        if not (isinstance(params, list) and len(params) == 2 and isinstance(params[0], dict) and set(params[0]) == {"to", "data"} and isinstance(params[0]["to"], str) and ADDRESS_RE.fullmatch(params[0]["to"]) and isinstance(params[0]["data"], str) and HEX_RE.fullmatch(params[0]["data"]) and len(params[0]["data"]) <= 202 and isinstance(params[1], str) and BLOCK_TAG_RE.fullmatch(params[1])):
            raise ValueError("eth_call must be bounded, with to, data and a safe block tag")


    elif method == "eth_getStorageAt":
        if not (isinstance(params, list) and len(params) == 3
                and isinstance(params[0], str) and ADDRESS_RE.fullmatch(params[0])
                and isinstance(params[1], str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", params[1])
                and isinstance(params[2], str) and BLOCK_TAG_RE.fullmatch(params[2])):
            raise ValueError("getStorageAt requires a valid address, exact bytes32 slot, and safe block tag")
    elif method == "eth_getBlockByNumber":
        if not (isinstance(params, list) and len(params) == 2
                and isinstance(params[0], str) and BLOCK_TAG_RE.fullmatch(params[0])
                and params[1] is False):
            raise ValueError("getBlockByNumber requires a safe block tag and false fullTx")


def rpc(network_name, method, params=None, transport=None):
    if network_name not in NETWORKS:
        raise ValueError("Unknown or unapproved network")
    if params is None:
        params = []
    validate_rpc_request(method, params)
    expected = NETWORKS[network_name]
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    if transport is not None:  # offline unit tests: injectable trusted mock
        response = transport(expected.url, payload)
    else:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        req = urllib.request.Request(expected.url, data=encoded, headers={"Content-Type":"application/json", "Accept":"application/json", "User-Agent":"AirdropLab-ReadOnly/1.0"}, method="POST")
        client = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with client.open(req, timeout=7) as resp:
                if resp.status != 200:
                    raise RPCError(f"HTTP {resp.status}")
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RPCError("RPC response exceeded size limit")
                response = json.loads(raw)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            raise RPCError(f"RPC unavailable or malformed: {type(e).__name__}") from e
    if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != 1:
        raise RPCError("Unexpected JSON-RPC envelope")
    if "error" in response:
        raise RPCError("RPC returned an error")
    result = response.get("result")
    if method == "eth_getBlockByNumber":
        if not isinstance(result, dict) or not isinstance(result.get("number"), str) or not isinstance(result.get("timestamp"), str) or not isinstance(result.get("hash"), str):
            raise RPCError("Invalid block header")
        return result
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RPCError("RPC returned invalid result")
    return result


def parse_uint_hex(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise RPCError("Expected unsigned hex quantity")
    return int(value, 16)


def check_chain(network_name, transport=None):
    cfg = NETWORKS[network_name]
    observed = parse_uint_hex(rpc(network_name, "eth_chainId", [], transport=transport))
    if observed != cfg.chain_id:
        raise RPCError(f"Chain mismatch on {network_name}: expected {cfg.chain_id}, observed {observed}")
    return observed


def probe(network_name, transport=None):
    cfg = NETWORKS[network_name]
    observed = check_chain(network_name, transport=transport)
    block = parse_uint_hex(rpc(network_name, "eth_blockNumber", [], transport=transport))
    gas_wei = parse_uint_hex(rpc(network_name, "eth_gasPrice", [], transport=transport))
    # Gas proxy for 21k units only. NOT an actual estimated transfer fee, especially on rollups.
    proxy = Decimal(gas_wei) * Decimal(21000) / Decimal(10**18)
    return {"checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "network": network_name, "phase": cfg.phase, "chain_id_verified": observed,
            "latest_block": block, "reported_gas_price_gwei": str(Decimal(gas_wei)/Decimal(10**9)),
            "21000_gas_price_proxy_in_fee_symbol": str(proxy), "fee_symbol":cfg.fee_symbol,
            "status":"RPC_CHECK_PASSED_READ_ONLY", "warning":"This is a 21000-gas proxy, not a quote or reward eligibility. No wallet or transactions."}


def main():
    ap = argparse.ArgumentParser(description="Allowlisted read-only EVM network check")
    ap.add_argument("--network", choices=["all-testnets", *NETWORKS], default="all-testnets")
    args = ap.parse_args()
    names = [n for n in NETWORKS if NETWORKS[n].phase == "TESTNET"] if args.network == "all-testnets" else [args.network]
    outputs = []
    for n in names:
        try:
            outputs.append(probe(n))
        except (RPCError, ValueError) as e:
            outputs.append({"network": n, "status":"NOT_VERIFIED", "error":str(e)})
    print(json.dumps(outputs, ensure_ascii=False, indent=2))
    return 0 if all(x["status"] == "RPC_CHECK_PASSED_READ_ONLY" for x in outputs) else 2

if __name__ == "__main__":
    sys.exit(main())
