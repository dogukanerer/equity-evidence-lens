#!/usr/bin/env python3
"""Equity Evidence Lens V3: historical daily Base observations and static Pages.

Public-chain reads only. Uses immutable historical site records and changes generated
site files only if complete, validated new observations exist. No wallet or orders.
Existing GitHub Pages deployment is never controlled from this Python program.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from decimal import Decimal, localcontext
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'engine'))

from equity_lens import EvidenceError, compare, read_asset
from safe_rpc import RPCError, check_chain, parse_uint_hex, rpc
from publish_site import validate_live, stable_json, public_page
from stock_integrity import ASSETS, BASE_ASSETS_SOURCE

SCHEMA = 'equity-lens-public-history-1'
MAX_HISTORY_BYTES = 5_000_000
MAX_SNAPSHOTS = 730
HEX64 = re.compile(r'0x[0-9a-fA-F]{64}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
FILES = ['data/earlier.json','data/later.json','data/comparison.json']


def utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError('Invalid UTC time')
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as ex:
        raise EvidenceError('Invalid time format') from ex
    if d.tzinfo is None:
        raise EvidenceError('Timestamp without timezone')
    return d.astimezone(timezone.utc)


def read_json_bounded(file: Path, bound=MAX_HISTORY_BYTES):
    if not file.is_file() or file.stat().st_size > bound:
        raise EvidenceError(f'Absent/oversized public input: {file.name}')
    try:
        return json.loads(file.read_text(encoding='utf-8'))
    except (UnicodeError, ValueError) as ex:
        raise EvidenceError('Invalid JSON') from ex


def to_engine(pub):
    """Reconstruct only validated fields to compare; never infer hidden attributes."""
    s = dict(pub)
    s['schema_version'] = 'equity-lens-2'
    s['block'] = {**pub['block'], 'block_tag': hex(pub['block']['number'])}
    return s


def validate_public(pub):
    if not isinstance(pub, dict) or pub.get('schema_version') != 'equity-lens-public-snapshot-1':
        raise EvidenceError('Unexpected public snapshot schema')
    if pub.get('chain_id') != 8453 or pub.get('mode') != 'LIVE_BASE_MAINNET_READ_ONLY':
        raise EvidenceError('Synthetic/wrong chain public record')
    if pub.get('source_url') != BASE_ASSETS_SOURCE:
        raise EvidenceError('Unknown asset source')
    block = pub.get('block')
    if not isinstance(block, dict) or not isinstance(block.get('hash'), str) or not HEX64.fullmatch(block['hash']):
        raise EvidenceError('Invalid block hash')
    if not isinstance(block.get('number'), int) or isinstance(block.get('number'), bool) or block['number'] < 1:
        raise EvidenceError('Invalid block height')
    if utc(block['block_timestamp_utc']) > utc(pub['captured_at_utc']):
        raise EvidenceError('Block timestamp after capture')
    # Reuse prior audited public export validation for addresses, code hashes,
    # status READ_OK, numeric raw supply, decimals, and exact supply units.
    canonical = validate_live(to_engine(pub))
    if canonical['block']['hash'] != block['hash'].lower():
        raise EvidenceError('Block hash mismatch')
    return canonical


def validate_manifest(root):
    m = read_json_bounded(root / 'data' / 'manifest.json', 100_000)
    if not isinstance(m, dict) or m.get('chain_id') != 8453:
        raise EvidenceError('Wrong source manifest chain')
    hashes = m.get('files_sha256')
    if not isinstance(hashes, dict):
        raise EvidenceError('Missing source hashes')
    required = {'earlier.json','later.json','comparison.json'}
    if not required.issubset(hashes):
        raise EvidenceError('Missing baseline integrity fields')
    allowed = required | {'history.json'}
    if set(hashes) - allowed:
        raise EvidenceError('Unknown file in manifest')
    for name, digest in hashes.items():
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise EvidenceError('Bad SHA-256 value')
        f = root / 'data' / name
        if not f.is_file() or f.stat().st_size > MAX_HISTORY_BYTES:
            raise EvidenceError('Source file absent/oversized')
        if hashlib.sha256(f.read_bytes()).hexdigest() != digest:
            raise EvidenceError('SOURCE_MANIFEST_HASH_MISMATCH: ' + name)
    return m


def history_from_site(root):
    validate_manifest(root)
    a = validate_public(read_json_bounded(root / 'data/earlier.json', 200_000))
    b = validate_public(read_json_bounded(root / 'data/later.json', 200_000))
    history_file = root / 'data/history.json'
    if history_file.exists():
        history = read_json_bounded(history_file)
        if not isinstance(history, dict) or history.get('schema_version') != SCHEMA or history.get('chain_id') != 8453:
            raise EvidenceError('History schema/chain invalid')
        snapshots = history.get('snapshots')
        if not isinstance(snapshots, list) or len(snapshots) < 2 or len(snapshots) > MAX_SNAPSHOTS:
            raise EvidenceError('History size invalid')
        clean = [validate_public(x) for x in snapshots]
        if any(clean[i]['block']['number'] >= clean[i+1]['block']['number'] for i in range(len(clean)-1)):
            raise EvidenceError('History blocks not strictly increasing')
        if clean[-2:] != [a, b]:
            raise EvidenceError('Current site data do not match history tip')
        return clean
    if a['block']['number'] >= b['block']['number'] or a['block']['hash'] == b['block']['hash']:
        raise EvidenceError('Invalid initial chronological pair')
    return [a, b]


def capture_matured(*, transport=None, lag_blocks=32, now=None):
    """Observe one mature-ish height (lag is NOT a finality guarantee)."""
    if lag_blocks < 1 or lag_blocks > 1000:
        raise EvidenceError('Invalid lag')
    check_chain('base-mainnet', transport=transport)
    head = parse_uint_hex(rpc('base-mainnet', 'eth_blockNumber', [], transport=transport))
    if head <= lag_blocks:
        raise EvidenceError('Chain head too low')
    h = head - lag_blocks
    tag = hex(h)
    header = rpc('base-mainnet', 'eth_getBlockByNumber', [tag, False], transport=transport)
    if parse_uint_hex(header['number']) != h or not HEX64.fullmatch(header['hash']):
        raise EvidenceError('Wrong block header')
    timestamp = parse_uint_hex(header['timestamp'])
    observed = now or datetime.now(timezone.utc)
    blocktime = datetime.fromtimestamp(timestamp, timezone.utc)
    if blocktime > observed + timedelta(seconds=30) or blocktime < observed - timedelta(hours=2):
        raise EvidenceError('Chain block is in future or excessively old')
    blockhash = header['hash'].lower()
    assets = [read_asset(t, tag, transport=transport) for t in ASSETS]
    if any(a['status'] != 'READ_OK' for a in assets):
        raise EvidenceError('INCOMPLETE_NEW_SNAPSHOT_NO_PUBLICATION')
    # A shallow reorg or changed provider response cannot become a published record.
    again = rpc('base-mainnet', 'eth_getBlockByNumber', [tag, False], transport=transport)
    if again['hash'].lower() != blockhash or parse_uint_hex(again['number']) != h:
        raise EvidenceError('Block identity changed while collecting')
    full = {'schema_version':'equity-lens-2','mode':'LIVE_BASE_MAINNET_READ_ONLY',
            'captured_at_utc': observed.isoformat(), 'chain_id':8453,
            'source_url': BASE_ASSETS_SOURCE,
            'block': {'number':h,'hash':blockhash, 'block_timestamp_utc':blocktime.isoformat(), 'block_tag':tag},
            'assets':assets}
    return validate_live(full)


def append_snapshot(series, new):
    new = validate_public(new)
    if series[-1]['block']['number'] == new['block']['number'] and series[-1]['block']['hash'] == new['block']['hash']:
        return series, False
    if new['block']['number'] <= series[-1]['block']['number']:
        raise EvidenceError('CHAIN_HEIGHT_DID_NOT_ADVANCE_OR_REORG')
    if utc(new['block']['block_timestamp_utc']) < utc(series[-1]['block']['block_timestamp_utc']):
        raise EvidenceError('BLOCK_TIME_MOVED_BACKWARDS')
    if len(series) >= MAX_SNAPSHOTS:
        raise EvidenceError('HISTORY_CAPACITY_EXCEEDED_MANUAL_REVIEW_REQUIRED')
    return series + [new], True


def make_chart(ticker, series):
    """Small static SVG; dots are observations, NOT continuous or price data."""
    unit_series = []
    decs = set()
    for s in series:
        a = next(x for x in s['assets'] if x['ticker'] == ticker)
        decs.add(a['decimals_reported'])
        unit_series.append((s['block']['number'], int(a['total_supply_raw']), a['decimals_reported']))
    if len(decs) != 1:
        return '<p class="warning">Token decimals changed; continuous supply line suppressed.</p>'
    with localcontext() as ctx:
        ctx.prec = 110
        vals = [Decimal(v) / (Decimal(10) ** dec) for _,v,dec in unit_series]
        small, large = min(vals), max(vals)
        span = large - small
        xs = [34 + (i / (len(vals)-1)) * 630 for i in range(len(vals))]
        ys = [90.0 if not span else 155 - 120 * float((v-small)/span) for v in vals]
    circles = ''.join(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.2" fill="#76e7cf"/>' for x,y in zip(xs,ys))
    points = ' '.join(f'{x:.2f},{y:.2f}' for x,y in zip(xs,ys))
    # dashed between samples, to stress intermittent, non-continuous observation
    start = html.escape(series[0]['block']['block_timestamp_utc'][:10])
    end = html.escape(series[-1]['block']['block_timestamp_utc'][:10])
    return (f'<svg viewBox="0 0 690 200" role="img" aria-label="{html.escape(ticker)}: {len(vals)} sampled supply points"'
            f' width="100%" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M34 155 H665" fill="none" stroke="#35546b"/>'
            '<path d="M34 35 V155" fill="none" stroke="#35546b"/>'
            f'<polyline points="{points}" fill="none" stroke="#76e7cf" stroke-width="2" stroke-dasharray="5 5"/>'
            f'{circles}'
            f'<text x="34" y="190" font-size="11" fill="#bdd3e3">{start}</text>'
            f'<text x="590" y="190" font-size="11" fill="#bdd3e3">{end}</text>'
            '</svg>')


def chart_section(series):
    cards = []
    for ticker in ASSETS:
        first = next(x for x in series[0]['assets'] if x['ticker'] == ticker)
        last = next(x for x in series[-1]['assets'] if x['ticker'] == ticker)
        changed = first['total_supply_raw'] != last['total_supply_raw']
        cards.append('<article class="hist-card">'
                     f'<h3>{html.escape(ticker)}</h3>'
                     f'<p><strong>{html.escape(last["token_units_reported"])}</strong> bildirilen token birimi</p>'
                     f'{make_chart(ticker, series)}'
                     f'<p class="hist-mini">{len(series)} örnek · başlangıç {html.escape(first["token_units_reported"])} · son {html.escape(last["token_units_reported"])}</p>'
                     f'<p class="hist-mini">Başlangıçtan son gözleme arz: {"DEĞİŞTİ" if changed else "aynı"}. '
                     'Bu gözlem bir güvenlik sertifikası değildir.</p></article>')
    return ('<section class="history" aria-labelledby="history-title"><h2 id="history-title">'
            'Gözlenen arz geçmişi</h2>'
            '<p>Her nokta tek bir Base bloğunda alınmış <code>totalSupply()</code> gözlemidir; '
            'kesikli çizgi ara bloklardaki değişimi veya gerçek fiyat hareketini kanıtlamaz. '
            'Y ekseni farkları görünür kılmak için daraltılmış olabilir; sıfırdan başlamaz. '
            'Dolaşımdaki arz, hisse adedi, B20 ayarlaması veya USD değer değildir.</p>'
            '<div class="hist-grid">' + ''.join(cards) + '</div>'
            '<p><a href="data/history.json">Tüm tarihli kanıt kayıtları (JSON)</a> · '
            f'{len(series)} kayıt · geçmiş her çalışmada aynı bloklar yeniden yazılmaz.</p></section>')


def build_outputs(series):
    if len(series) < 2:
        raise EvidenceError('At least two sampled blocks required')
    older, newer = series[-2:]
    full_a, full_b = to_engine(older), to_engine(newer)
    report = compare(full_a, full_b)
    if any(r['status'] == 'INSUFFICIENT_EVIDENCE' for r in report['results']):
        raise EvidenceError('Incomplete comparison')
    results = [{'ticker': x['ticker'], 'address': x['address'], 'status':x['status'],
                'changes': x['changes'], 'raw_supply_delta': x['raw_supply_delta'],
                'raw_supply_pct_change': x['raw_supply_pct_change']} for x in report['results']]
    comp = {'schema_version':'equity-lens-public-comparison-1','chain_id':8453,
            'earlier_block':older['block'], 'later_block':newer['block'],
            'results':results,'interpretation':'Two observations only; not security certification or investment advice.'}
    history = {'schema_version':SCHEMA,'chain_id':8453,'source_url':BASE_ASSETS_SOURCE,
               'snapshots':series, 'note':'Sampling, not continuous surveillance; archive represents successive successful captures.'}
    doc = {'earlier.json':stable_json(older), 'later.json':stable_json(newer),
           'comparison.json':stable_json(comp), 'history.json':stable_json(history)}
    manifest = {'schema_version':'equity-lens-public-manifest-1','chain_id':8453,
                'files_sha256': {name:hashlib.sha256(b).hexdigest() for name,b in doc.items()},
                'scope':'Sanitized, public read-only Base observations; SHA256 is file integrity, not state proof.'}
    page = public_page(older, newer, report, manifest)
    # Prior V2 freshness wording is only accurate at build time. Make it fixed-date,
    # so future scheduled-run failures do not imply a currently fresh snapshot.
    repl = '<p>Son başarılı gözlem: ' + html.escape(newer['captured_at_utc']) + ' UTC etiketli kayıt · Bu rapor'
    page, n = re.subn(r'<p>(?:Yakın zamanda alınmış kayıt|ESKİ KAYIT — \d+ saat önce) · Bu rapor', repl, page)
    if n != 1:
        raise EvidenceError('Unexpected base page freshness markup; cannot safely publish')
    history_css = ('<style>.hist-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px}'
                   '.hist-card{background:#12243a;border:1px solid #294864;border-radius:17px;padding:18px;overflow:hidden}'
                   '.hist-mini{font-size:12px;color:#9db9d3}.history{margin-top:32px}.warning{color:#ffd69c}</style>')
    page = page.replace('</head>', history_css + '</head>')
    if page.count('</main>') != 1:
        raise EvidenceError('Unexpected page markup')
    page = page.replace('</main>', chart_section(series) + '</main>')
    page = page.replace('href="data/manifest.json">SHA-256 manifesti</a>',
                        'href="data/manifest.json">SHA-256 manifesti</a>')
    return {'index.html':page.encode('utf-8'), '.nojekyll':b'',
            **{'data/'+name:b for name,b in doc.items()},
            'data/manifest.json':stable_json(manifest)}


def write_outputs(root, contents):
    """Stage and validate before replacing; CI publishes only after a successful run."""
    with tempfile.TemporaryDirectory(prefix='equitylens-v3-') as d:
        staging = Path(d)
        for relative, bytes_ in contents.items():
            p = staging / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(bytes_)
        # Verify the generated bundle before touching live working tree.
        m = read_json_bounded(staging / 'data/manifest.json')
        for name, sha in m['files_sha256'].items():
            if hashlib.sha256((staging / 'data' / name).read_bytes()).hexdigest() != sha:
                raise EvidenceError('Staged artifact integrity check failed')
        if b'<html' not in (staging / 'index.html').read_bytes() or b'history.json' not in (staging / 'index.html').read_bytes():
            raise EvidenceError('Staged HTML invalid')
        for relative in contents:
            dest = root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / relative, dest)


def refresh(root: Path, *, capture_fn=capture_matured, check_only=False):
    existing = history_from_site(root)
    if check_only:
        return {'mode':'LOCAL_VALIDATION_NO_NETWORK','history_samples':len(existing),
                'latest_block':existing[-1]['block']['number'], 'written':False}
    new = capture_fn()
    updated, appended = append_snapshot(existing, new)
    if not appended:
        return {'mode':'UNCHANGED_ALREADY_SAMPLED','history_samples':len(existing),
                'latest_block':existing[-1]['block']['number'], 'written':False}
    outputs = build_outputs(updated)
    write_outputs(root, outputs)
    return {'mode':'REFRESHED_LIVE_BASE_READ_ONLY','history_samples':len(updated),
            'latest_block':updated[-1]['block']['number'],
            'statuses':{x['ticker']:'READ_OK' for x in updated[-1]['assets']},
            'new_public_files':len(outputs), 'wallet_or_order_actions':False,'written':True}


def main(argv=None):
    p = argparse.ArgumentParser(description='History snapshots + static charts, read-only Base RPC')
    p.add_argument('--site-root', type=Path, default=BASE)
    p.add_argument('--check-only', action='store_true', help='Validate current site history and manifest WITHOUT network or modifying files')
    args=p.parse_args(argv)
    try:
        print(json.dumps(refresh(args.site_root, check_only=args.check_only), indent=2, ensure_ascii=False))
        return 0
    except (EvidenceError, RPCError, ValueError, OSError, TypeError, KeyError, OverflowError) as exc:
        print(f'EQUITY_REFRESH_STOPPED_NO_PUBLISH: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
