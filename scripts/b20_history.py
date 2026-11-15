#!/usr/bin/env python3
"""Fail-closed, read-only B20 Asset evidence history integrated with Equity Lens.

Consumes the *exact* block from the already verified public Base observation;
never asks for a separate latest block. All reads are eth_call at one pinned block.
No signing, wallet, trading, credentials, deployment or push.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'engine'))
from equity_lens import EvidenceError
from safe_rpc import check_chain, rpc, parse_uint_hex
from stock_integrity import ASSETS
from scripts.b20_native_probe import SELECTORS, normalize_asset_values, read_word, header_at, ProbeError
from scripts.refresh_site import history_from_site, utc

SCHEMA = 'equity-lens-b20-snapshot-1'
HISTORY_SCHEMA = 'equity-lens-b20-history-1'
MANIFEST_SCHEMA = 'equity-lens-b20-manifest-1'
FILES = ('b20_latest.json', 'b20_history.json', 'b20_manifest.json')
BEGIN = '<!-- EQUITY_B20_BEGIN -->'
END = '<!-- EQUITY_B20_END -->'
SECTION_ID = 'b20-native-evidence-v1'
MAX_HISTORY = 730
MAX_BYTES = 4_000_000
HEX64 = re.compile(r'0x[0-9a-fA-F]{64}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')


def jbytes(x):
    return (json.dumps(x, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path, limit=MAX_BYTES):
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise EvidenceError('B20_MISSING_OR_OVERSIZED: ' + path.name)
    try:
        return json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise EvidenceError('B20_BAD_JSON: ' + path.name) from exc


def target(core):
    if len(core) < 2:
        raise EvidenceError('B20_CORE_HISTORY_TOO_SHORT')
    block = core[-1]['block']
    if not isinstance(block.get('number'), int) or isinstance(block.get('number'), bool) or block['number'] <= 0:
        raise EvidenceError('B20_BAD_CORE_BLOCK')
    if not isinstance(block.get('hash'), str) or not HEX64.fullmatch(block['hash']):
        raise EvidenceError('B20_BAD_CORE_HASH')
    return block


def validate_record(record, core_by_block):
    if not isinstance(record, dict) or record.get('schema_version') != SCHEMA or record.get('chain_id') != 8453 or record.get('mode') != 'LIVE_BASE_MAINNET_READ_ONLY':
        raise EvidenceError('B20_WRONG_SCHEMA_CHAIN_OR_MODE')
    block = record.get('block')
    if not isinstance(block, dict) or not isinstance(block.get('number'), int) or isinstance(block.get('number'), bool) or not isinstance(block.get('hash'), str) or not HEX64.fullmatch(block['hash']):
        raise EvidenceError('B20_INVALID_BLOCK')
    core_sample = core_by_block.get(block['number'])
    core_block = core_sample.get('block') if isinstance(core_sample, dict) else None
    if core_block is None or core_block['hash'].lower() != block['hash'].lower():
        raise EvidenceError('B20_BLOCK_MISMATCH_WITH_CORE_HISTORY')
    timestamp = block.get('timestamp_utc')
    if timestamp != core_block['block_timestamp_utc']:
        # No implicit rounding of time stamps or comparing a different block.
        raise EvidenceError('B20_BLOCK_TIME_MISMATCH')
    stamp = int(utc(timestamp).timestamp())
    assets = record.get('assets')
    if not isinstance(assets, list) or len(assets) != len(ASSETS) or {x.get('ticker') for x in assets if isinstance(x,dict)} != set(ASSETS):
        raise EvidenceError('B20_MISSING_OR_DUPLICATE_ASSETS')
    core_assets = {a['ticker']:a for a in core_sample.get('assets', [])}
    if set(core_assets) != set(ASSETS):
        raise EvidenceError('B20_CORE_ASSET_SET_MISMATCH')
    for item in assets:
        ticker = item['ticker']
        if item.get('address') != ASSETS[ticker]:
            raise EvidenceError('B20_ASSET_ADDRESS_MISMATCH')
        try:
            raw = { 'total_supply_raw':int(item['total_supply_raw']), 'decimals':item['decimals'],
                    'ui_multiplier':int(item['ui_multiplier_wad']), 'new_ui_multiplier':int(item['new_ui_multiplier_wad']),
                    'effective_at':item['effective_at_unix'], 'total_supply_ui_raw':int(item['total_supply_ui_raw']) }
            for k in ('total_supply_raw','total_supply_ui_raw','ui_multiplier_wad','new_ui_multiplier_wad'):
                if str(raw[{'ui_multiplier_wad':'ui_multiplier','new_ui_multiplier_wad':'new_ui_multiplier'}.get(k,k)]) != item[k]:
                    raise ValueError('noncanonical number')
            computed = normalize_asset_values(raw, stamp)
        except (ValueError, TypeError, KeyError, ProbeError) as exc:
            raise EvidenceError('B20_INVALID_ASSET_MATH') from exc
        if {k:item.get(k) for k in computed} != computed:
            raise EvidenceError('B20_TAMPERED_DERIVED_VALUES')
        anchor = core_assets[ticker]
        if item['total_supply_raw'] != anchor['total_supply_raw'] or item['decimals'] != anchor['decimals_reported']:
            raise EvidenceError('B20_CORE_SUPPLY_OR_DECIMALS_DISAGREE')
    return record


def load_existing(root, core):
    data = root / 'data'
    present = [ (data / f).exists() for f in FILES ]
    if not any(present):
        return []
    if not all(present):
        raise EvidenceError('B20_PARTIAL_PUBLIC_EVIDENCE')
    m = read_json(data / 'b20_manifest.json', 50_000)
    if not isinstance(m, dict) or m.get('schema_version') != MANIFEST_SCHEMA or m.get('chain_id') != 8453 or set(m.get('files_sha256', {})) != {'b20_latest.json','b20_history.json'}:
        raise EvidenceError('B20_INVALID_MANIFEST')
    for fname, expected in m['files_sha256'].items():
        if not isinstance(expected,str) or not SHA.fullmatch(expected):
            raise EvidenceError('B20_BAD_SHA256')
        if digest((data/fname).read_bytes()) != expected:
            raise EvidenceError('B20_MANIFEST_INTEGRITY_FAILURE')
    h = read_json(data / 'b20_history.json')
    if not isinstance(h,dict) or h.get('schema_version') != HISTORY_SCHEMA or h.get('chain_id') != 8453:
        raise EvidenceError('B20_HISTORY_SCHEMA_INVALID')
    samples = h.get('snapshots')
    if not isinstance(samples,list) or not 1 <= len(samples) <= MAX_HISTORY:
        raise EvidenceError('B20_HISTORY_SIZE_INVALID')
    by_block = {x['block']['number']:x for x in core}
    for sample in samples:
        validate_record(sample,by_block)
    if any(a['block']['number'] >= b['block']['number'] for a,b in zip(samples,samples[1:])):
        raise EvidenceError('B20_NOT_STRICTLY_INCREASING')
    if read_json(data/'b20_latest.json') != samples[-1]:
        raise EvidenceError('B20_LATEST_HISTORY_MISMATCH')
    return samples


def collect_pinned(core_block, *, transport=None):
    if check_chain('base-mainnet',transport=transport) != 8453:
        raise EvidenceError('B20_WRONG_CHAIN')
    blockid = core_block['number']
    tag = hex(blockid)
    n, h, ts = header_at(tag,transport)
    if n != blockid or h.lower() != core_block['hash'].lower() or datetime.fromtimestamp(ts,timezone.utc) != utc(core_block['block_timestamp_utc']):
        raise EvidenceError('B20_CORE_BLOCK_IDENTITY_MISMATCH')
    observations = []
    for ticker, address in ASSETS.items():
        vals = {field:read_word(address, selector,tag,transport) for field, selector in SELECTORS.items()}
        derived = normalize_asset_values(vals,ts)
        observations.append({'ticker':ticker,'address':address,**derived})
    n2,h2,ts2 = header_at(tag,transport)
    if (n,h,ts) != (n2,h2,ts2):
        raise EvidenceError('B20_BLOCK_REORG_DURING_READ')
    return {'schema_version':SCHEMA,'chain_id':8453,'mode':'LIVE_BASE_MAINNET_READ_ONLY',
            'block':{'number':n,'hash':h,'timestamp_utc':datetime.fromtimestamp(ts,timezone.utc).isoformat()},
            'assets':observations,
            'scope':'Read-only native B20 Asset calls; raw supply and multiplier-adjusted UI supply are different concepts.'}


def render_section(history):
    latest = history[-1]
    prior = history[-2] if len(history)>1 else None
    pmap = {a['ticker']:a for a in prior['assets']} if prior else {}
    esc=lambda x: html.escape(str(x),quote=True)
    cards=[]
    for item in latest['assets']:
        ticker=item['ticker']
        raw=Decimal(item['raw_supply_units']); ui=Decimal(item['ui_adjusted_supply_units']); delta=ui-raw
        pending=item['pending_multiplier_update']
        old=pmap.get(ticker)
        if old is None:
            status='İlk B20 gözlemi: henüz zaman içi karşılaştırma yok.'
        else:
            changed=[key for key in ('total_supply_raw','ui_multiplier_wad','total_supply_ui_raw','effective_at_unix','new_ui_multiplier_wad') if item[key]!=old[key]]
            status=('Örnekler arasında değişen alanlar: '+', '.join(changed)) if changed else 'İki B20 gözleminde karşılaştırılan alanlar aynı.'
        when= (datetime.fromtimestamp(item['effective_at_unix'],timezone.utc).isoformat() if pending else None)
        scheduled=(f'<p><strong>İleri tarihli multiplier:</strong> {esc(item["new_ui_multiplier_wad"])} WAD · etkinleşme {esc(when)}</p>' if pending
                   else '<p>Bu blokta <strong>bekleyen multiplier değişikliği gözlenmedi</strong>. Gelecekte değişmeyeceği anlamına gelmez.</p>')
        cards.append('<article class="b20-card">'
            f'<h3>{esc(ticker)}</h3>'
            f'<p><strong>Ham arz:</strong> <code>{esc(item["raw_supply_units"])}</code></p>'
            f'<p><strong>UI uyarlanmış arz:</strong> <code>{esc(item["ui_adjusted_supply_units"])}</code></p>'
            f'<p><strong>Multiplier:</strong> <code>{esc(item["ui_multiplier_factor"])}</code></p>'
            f'<p><strong>UI–ham farkı:</strong> <code>{esc(format(delta,"f"))}</code> token birimi</p>'
            f'{scheduled}<p class="b20-muted">{esc(status)}</p>'
            '</article>')
    return (BEGIN+f'<section id="{SECTION_ID}" aria-labelledby="b20-h">'
       '<style>#b20-native-evidence-v1{margin-top:36px;padding:22px;background:#101e2c;color:#e5f2fc;border:1px solid #355169;border-radius:16px}'
       '#b20-native-evidence-v1 .b20-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}'
       '#b20-native-evidence-v1 .b20-card{border:1px solid #395066;border-radius:13px;background:#192a3b;padding:14px;overflow-wrap:anywhere}'
       '#b20-native-evidence-v1 code{overflow-wrap:anywhere}#b20-native-evidence-v1 a{color:#8ed8e9}'
       '#b20-native-evidence-v1 .b20-muted{font-size:13px;color:#b5c6d5}</style>'
       '<h2 id="b20-h">B20 yerel varlık kanıtları</h2>'
       '<p>Bu varlıklar B20 native precompile yapısındadır; klasik ERC-1967 proxy alanları yetki denetiminin karşılığı değildir. '
       'Ham <code>totalSupply()</code> ile <code>totalSupplyUI()</code> farklı olabilir. Bunlar fiyat, alım gücü, dolaşımdaki arz veya hissedar hakkı değildir.</p>'
       f'<p>Son B20 gözlemi: Base blok <strong>#{latest["block"]["number"]}</strong> · '
       f'<strong>{len(history)}</strong> doğrulanmış B20 örneği.</p>'
       '<div class="b20-grid">'+''.join(cards)+'</div>'
       '<p class="b20-muted">Multiplier değişiklikleri planlı olarak etkinleşebilir; ara bloklar, yönetici/roller, transfer politikaları, pause durumları ve fiyat analiz edilmemiştir. '
       'Tek bloktaki RPC okumaları kriptografik durum ispatı değildir.</p>'
       '<p><a href="data/b20_latest.json">Son B20 kanıtı</a> · '
       '<a href="data/b20_history.json">B20 geçmişi</a> · '
       '<a href="data/b20_manifest.json">B20 bütünlük özeti</a></p>'
       '</section>'+END)


def html_with_section(original: str, section: str):
    if original.count('</main>')!=1:
        raise EvidenceError('B20_SITE_MAIN_MISSING_OR_DUPLICATE')
    if BEGIN in original or END in original:
        if original.count(BEGIN)!=1 or original.count(END)!=1:
            raise EvidenceError('B20_SITE_MARKER_CORRUPT')
        start=original.index(BEGIN)
        end=original.index(END)+len(END)
        if start >= end:
            raise EvidenceError('B20_SITE_MARKER_ORDER')
        return original[:start]+section+original[end:]
    if SECTION_ID in original:
        raise EvidenceError('B20_UNMANAGED_SECTION_EXISTS')
    return original.replace('</main>',section+'</main>')


def make_output(root:Path,samples):
    latest=samples[-1]
    history={'schema_version':HISTORY_SCHEMA,'chain_id':8453,'snapshots':samples,
             'disclaimer':'Only sampled Base blocks. No inference about missed events, issuance, rights, custody, price, or safety.'}
    files={'data/b20_latest.json':jbytes(latest),'data/b20_history.json':jbytes(history)}
    manifest={'schema_version':MANIFEST_SCHEMA,'chain_id':8453,
              'files_sha256':{name.split('/')[-1]:digest(buf) for name,buf in files.items()},
              'scope':'Integrity of public B20 observations, not cryptographic blockchain state proof.'}
    files['data/b20_manifest.json']=jbytes(manifest)
    current = root/'index.html'
    if not current.is_file() or current.stat().st_size>MAX_BYTES:
        raise EvidenceError('B20_SITE_MISSING_OR_TOO_LARGE')
    page=current.read_text(encoding='utf-8')
    files['index.html']=html_with_section(page,render_section(samples)).encode('utf-8')
    return files


def stage_write(root, outputs):
    with tempfile.TemporaryDirectory(prefix='equity-b20-') as d:
        stage=Path(d)
        for name,content in outputs.items():
            target=stage/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(content)
        m=json.loads((stage/'data/b20_manifest.json').read_bytes())
        for name,want in m['files_sha256'].items():
            if digest((stage/'data'/name).read_bytes())!=want:
                raise EvidenceError('B20_STAGE_MANIFEST_MISMATCH')
        text=(stage/'index.html').read_text(encoding='utf-8')
        if text.count(f'id="{SECTION_ID}"')!=1 or text.count(BEGIN)!=1 or text.count(END)!=1:
            # id used once in <section> and once in CSS selector.
            raise EvidenceError('B20_STAGE_HTML_INVALID')
        # No output changes are committed or published by this script.
        for name in outputs:
            dest=root/name
            dest.parent.mkdir(parents=True,exist_ok=True)
            os.replace(stage/name,dest)


def process(root:Path,*,check_only=False, collector=collect_pinned):
    core=history_from_site(root)
    previous=load_existing(root,core)
    if check_only:
        return {'mode':'B20_LOCAL_CHECK_NO_NETWORK','core_observations':len(core),
                'b20_observations':len(previous),'latest_core_block':core[-1]['block']['number'],
                'written':False}
    expected=target(core)
    if previous and previous[-1]['block']['number']==expected['number']:
        return {'mode':'B20_ALREADY_SAMPLED_NO_WRITE','b20_observations':len(previous),'written':False}
    if previous and previous[-1]['block']['number']>expected['number']:
        raise EvidenceError('B20_HISTORY_AHEAD_OF_CORE')
    record=collector(expected)
    validate_record(record,{x['block']['number']:x for x in core})
    if len(previous)>=MAX_HISTORY:
        raise EvidenceError('B20_HISTORY_AT_CAPACITY_NO_TRUNCATION')
    updated=previous+[record]
    outputs=make_output(root,updated)
    stage_write(root,outputs)
    return {'mode':'B20_PINNED_LIVE_READ_ONLY_PUBLISHED_TO_WORKTREE','block':expected['number'],
            'b20_observations':len(updated),'ui_supply_difference_assets':[
                a['ticker'] for a in record['assets'] if a['total_supply_raw']!=a['total_supply_ui_raw']],
            'written':True,'wallet_or_order_actions':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description='B20 evidence history; pinned to verified published Base snapshot')
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--site-root',type=Path,default=ROOT)
    args=parser.parse_args(argv)
    try:
        print(json.dumps(process(args.site_root,check_only=args.check_only),indent=2,ensure_ascii=False))
        return 0
    except (EvidenceError,ProbeError,OSError,ValueError,TypeError,KeyError) as exc:
        print('B20_HISTORY_STOPPED_NO_PUBLISH: '+type(exc).__name__+': '+str(exc),file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
