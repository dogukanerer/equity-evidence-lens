#!/usr/bin/env python3
"""B20 Asset RPC diagnostics for Equity Evidence Lens.

Read-only ETH_CALL on allowlisted Base mainnet assets; single pinned block. 
Does not modify website, history, Github Actions, or trading/wallet state.
B20 native precompiles are not conventional ERC-1967 proxies.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
from safe_rpc import check_chain, rpc, parse_uint_hex  # noqa: E402
from stock_integrity import ASSETS  # noqa: E402

# Selectors cross-checked against base/base-std Cobalt reference, link in README.
# decimals, totalSupply are ERC-20 compatible; others are B20 Asset/ERC-8056.
SELECTORS = {
    'total_supply_raw': '0x18160ddd',
    'decimals': '0x313ce567',
    'ui_multiplier': '0xa60bf13d',
    'new_ui_multiplier': '0xdc767007',
    'effective_at': '0x97a4064f',
    'total_supply_ui_raw': '0x9bea6429',
}
WORD = re.compile(r'0x[0-9a-fA-F]{64}\Z')
HASH = re.compile(r'0x[0-9a-fA-F]{64}\Z')
WAD = 10**18
MAX_LAG = 512

class ProbeError(Exception):
    pass


def decode_word(value, label):
    if not isinstance(value, str) or not WORD.fullmatch(value):
        raise ProbeError('BAD_ABI_UINT256:' + label)
    return int(value, 16)


def read_word(address, selector, tag, transport=None):
    result = rpc('base-mainnet', 'eth_call', [{'to': address, 'data': selector}, tag], transport=transport)
    return decode_word(result, selector)


def header_at(tag, transport=None):
    h = rpc('base-mainnet', 'eth_getBlockByNumber', [tag, False], transport=transport)
    if not isinstance(h, dict) or not HASH.fullmatch(h.get('hash', '')):
        raise ProbeError('BAD_BLOCK_HASH')
    number = parse_uint_hex(h['number'])
    stamp = parse_uint_hex(h['timestamp'])
    if stamp < 1 or stamp > 20000000000:
        raise ProbeError('BAD_BLOCK_TIMESTAMP')
    return number, h['hash'].lower(), stamp


def normalize_asset_values(raw, block_ts):
    if not isinstance(raw, dict) or set(raw) != set(SELECTORS):
        raise ProbeError('INCOMPLETE_B20_VALUES')
    supply = raw['total_supply_raw']
    dec = raw['decimals']
    current = raw['ui_multiplier']
    future = raw['new_ui_multiplier']
    at = raw['effective_at']
    supplied_ui = raw['total_supply_ui_raw']
    if any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in raw.values()):
        raise ProbeError('UNSIGNED_B20_VALUE_EXPECTED')
    if not (6 <= dec <= 18):
        raise ProbeError('UNSUPPORTED_B20_DECIMALS')
    if not (0 < current <= 2**128-1 and 0 < future <= 2**128-1):
        raise ProbeError('INVALID_B20_MULTIPLIER')
    expected_ui = supply * current // WAD
    if supplied_ui != expected_ui:
        raise ProbeError('UI_SUPPLY_IDENTITY_FAILED')
    # The time remains nonzero after a matured schedule; only future is pending.
    pending = at > block_ts
    if not pending and future != current:
        raise ProbeError('MATURED_B20_MULTIPLIER_INCONSISTENCY')
    with localcontext() as ctx:
        ctx.prec = 130
        raw_units = str(Decimal(supply)/Decimal(10**dec))
        ui_units = str(Decimal(supplied_ui)/Decimal(10**dec))
        mult = str(Decimal(current)/Decimal(WAD))
    return {
        'raw_supply_units': raw_units, 'ui_adjusted_supply_units': ui_units,
        'decimals': dec, 'total_supply_raw': str(supply),
        'total_supply_ui_raw': str(supplied_ui),
        'ui_multiplier_wad': str(current), 'ui_multiplier_factor': mult,
        'new_ui_multiplier_wad': str(future),
        'effective_at_unix': at, 'pending_multiplier_update': pending,
        'change_classification': 'SCHEDULED_UI_MULTIPLIER_PENDING' if pending else 'NO_LIVE_PENDING_UI_MULTIPLIER',
    }


def collect(*, transport=None, lag_blocks=32, now=None):
    if not isinstance(lag_blocks, int) or isinstance(lag_blocks, bool) or not 16 <= lag_blocks <= MAX_LAG:
        raise ProbeError('INVALID_LAG_BLOCKS')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ProbeError('NAIVE_CURRENT_TIME')
    if check_chain('base-mainnet', transport=transport) != 8453:
        raise ProbeError('WRONG_CHAIN')
    tip = parse_uint_hex(rpc('base-mainnet', 'eth_blockNumber', [], transport=transport))
    if tip <= lag_blocks:
        raise ProbeError('INSUFFICIENT_CHAIN_HEIGHT')
    tag = hex(tip - lag_blocks)
    block, blockhash, stamp = header_at(tag, transport)
    if block != tip - lag_blocks:
        raise ProbeError('WRONG_PINNED_BLOCK')
    age = now.timestamp() - stamp
    if age < -30 or age > 3600:
        raise ProbeError('STALE_OR_FUTURE_BLOCK')
    result_assets = []
    for symbol, address in ASSETS.items():
        vals = {name:read_word(address, sel, tag, transport) for name, sel in SELECTORS.items()}
        result_assets.append({'ticker': symbol, 'address': address, **normalize_asset_values(vals, stamp)})
    block2, hash2, stamp2 = header_at(tag, transport)
    if (block2, hash2, stamp2) != (block, blockhash, stamp):
        raise ProbeError('BLOCK_CHANGED_DURING_READ')
    return {
        'mode': 'LIVE_BASE_MAINNET_READ_ONLY_NO_FILE_WRITES',
        'chain_id': 8453, 'block': block, 'block_hash': blockhash,
        'block_timestamp_utc': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        'assets': result_assets,
        'method': 'B20_ASSET_READ_ONLY_ETH_CALL_SINGLE_PINNED_BLOCK',
        'not_checked': ['admin and role holder identity', 'policies', 'pause flags',
                        'issuer custody', 'all intermediate blocks', 'investment safety',
                        'price', 'airdrop eligibility'],
        'warning': ('These are Base RPC observations, not cryptographic state proofs. '
                    'B20 Asset is a native precompile, so ERC-1967 proxy checks are not its authority audit. '
                    'No wallet, transaction, site change, or GitHub deployment.'),
    }


def synthetic():
    out=[]
    for symbol,address in ASSETS.items():
        raw={'total_supply_raw': 123456700, 'decimals': 8, 'ui_multiplier':WAD,
             'new_ui_multiplier':WAD, 'effective_at':0,'total_supply_ui_raw':123456700}
        out.append({'ticker':symbol,'address':address,**normalize_asset_values(raw, 1700000000)})
    return {'mode':'OFFLINE_SYNTHETIC_NOT_LIVE', 'chain_id':8453,
            'block':123456,'assets':out, 'warning':'Synthetic example only. No RPC calls, no site changes, no transactions.'}


def main(argv=None):
    ap=argparse.ArgumentParser(description='Independent native B20 Asset read-only diagnostic; no website changes')
    group=ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--fixture',action='store_true',help='Offline synthetic example')
    group.add_argument('--live',action='store_true',help='Read official Base RPC; no private keys')
    ap.add_argument('--lag-blocks',type=int,default=32,help='Read a single prior block, default 32')
    args=ap.parse_args(argv)
    try:
        out=synthetic() if args.fixture else collect(lag_blocks=args.lag_blocks)
    except Exception as exc:
        print('B20_PROBE_STOPPED_NO_WEBSITE_CHANGES: '+str(exc),file=sys.stderr)
        return 2
    print(json.dumps(out,indent=2,ensure_ascii=False,allow_nan=False))
    return 0

if __name__=='__main__':
    sys.exit(main())
