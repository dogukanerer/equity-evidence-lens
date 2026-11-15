#!/usr/bin/env python3
"""Evidence-first V5 event review for Equity Evidence Lens.

OFFLINE ONLY. No network, wallets, secrets, trades or orders.
Reads validated, hash-checked historical observations from V3 and V4; builds
human-review events for sampled differences; never asserts causation or safety.
"""
from __future__ import annotations
import argparse
from decimal import Decimal, localcontext
import hashlib
import html
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'engine'))
from equity_lens import EvidenceError
from stock_integrity import ASSETS
from scripts.refresh_site import history_from_site
from scripts.proxy_monitor import read_prior

SCHEMA = 'equity-lens-change-review-1'
MANIFEST_SCHEMA = 'equity-lens-change-review-manifest-1'
SOURCE = ('history.json', 'manifest.json', 'proxy_history.json', 'proxy_manifest.json')
OUTPUT = ('alerts.json', 'alerts_manifest.json')
MAX_ALERTS = 2500
MAX_JSON_BYTES = 2_000_000
SECTION_MARKER = 'id="equity-alerts-v1"'


def jbytes(obj):
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def sha(buf):
    return hashlib.sha256(buf).hexdigest()


def inputs(root):
    """Reuses the original authenticated-record checks; no network operations."""
    core = history_from_site(root)
    proxies = read_prior(root, core)
    if not proxies:
        raise EvidenceError('PROXY_HISTORY_NOT_READY: complete V4 audit first')
    digests = {}
    for name in SOURCE:
        path = root / 'data' / name
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_JSON_BYTES * 3:
            raise EvidenceError('MISSING_OVERSIZED_SOURCE: ' + name)
        digests[name] = sha(path.read_bytes())
    return core, proxies, digests


def event(scope, kind, ticker, before, after, field, previous, current, meaning):
    asset = ASSETS[ticker]
    b, a = before['block'], after['block']
    if b['number'] >= a['number']:
        raise EvidenceError('NON_FORWARD_OBSERVATION_PAIR')
    payload = {'scope': scope, 'kind': kind, 'ticker': ticker, 'address': asset,
               'field': field, 'from_block': b['number'], 'to_block': a['number'],
               'from_block_hash': b['hash'], 'to_block_hash': a['hash'],
               'previous': previous, 'current': current,
               'label': 'MANUAL_REVIEW', 'meaning': meaning,
               'limitation': 'Only sampled values; event timing, cause and intervening changes are unknown.'}
    # Deterministic ID; reruns of identical source cannot manufacture new alerts.
    ident = sha(jbytes(payload))[:24]
    return {'event_id': ident, **payload}


def by_ticker(snapshot):
    return {item['ticker']: item for item in snapshot['assets']}


def supply_events(core):
    out = []
    for before, after in zip(core, core[1:]):
        old, new = by_ticker(before), by_ticker(after)
        for ticker in ASSETS:
            o, n = old[ticker], new[ticker]
            if o['decimals_reported'] != n['decimals_reported']:
                out.append(event('token','DECIMALS_CHANGED',ticker,before,after,
                    'decimals_reported',o['decimals_reported'],n['decimals_reported'],
                    'Reported unit precision changed. Do not compare displayed supply units without manual review.'))
            elif o['total_supply_raw'] != n['total_supply_raw']:
                prior_raw, next_raw = int(o['total_supply_raw']), int(n['total_supply_raw'])
                decs = o['decimals_reported']
                with localcontext() as ctx:
                    ctx.prec = 120
                    factor = Decimal(10) ** decs
                    before_units = format(Decimal(prior_raw) / factor, 'f')
                    after_units = format(Decimal(next_raw) / factor, 'f')
                kind = 'REPORTED_SUPPLY_INCREASED' if next_raw > prior_raw else 'REPORTED_SUPPLY_DECREASED'
                out.append(event('token',kind,ticker,before,after,'total_supply_raw',
                    {'raw':str(prior_raw),'decimals':decs,'units':before_units},
                    {'raw':str(next_raw),'decimals':decs,'units':after_units},
                    'Contract-reported totalSupply differs between sampled blocks. This does not prove issuance, redemption, underlying stock ownership or a security incident.'))
            if o['code_sha256'] != n['code_sha256']:
                out.append(event('token','TOKEN_RUNTIME_FINGERPRINT_CHANGED',ticker,before,after,
                    'code_sha256',o['code_sha256'],n['code_sha256'],
                    'Provider-reported deployed runtime code fingerprint changed between observations; cause and authorization are not assessed.'))
    return out


PROXY_FIELDS = (
    ('implementation_target_address','IMPLEMENTATION_TARGET_CHANGED'),
    ('target_runtime_sha256','IMPLEMENTATION_RUNTIME_FINGERPRINT_CHANGED'),
    ('pattern_observed','PROXY_PATTERN_CLASSIFICATION_CHANGED'),
    ('erc1167_canonical_target','ERC1167_TARGET_CHANGED'),
)
SLOT_FIELDS = ('implementation','admin','beacon')


def proxy_events(proxies):
    out = []
    for before, after in zip(proxies,proxies[1:]):
        old, new = by_ticker(before), by_ticker(after)
        for ticker in ASSETS:
            o, n = old[ticker], new[ticker]
            for key in SLOT_FIELDS:
                # Use interpreted address: canonical hex case differences are not events.
                x, y = o['eip1967_addresses'][key], n['eip1967_addresses'][key]
                if x != y:
                    out.append(event('proxy','ERC1967_'+key.upper()+'_SLOT_CHANGED',ticker,before,after,
                        'eip1967_addresses.'+key,x,y,
                        'Standard ERC-1967 slot changed between samples. A slot alone does not establish privileges, a malicious upgrade or the actual cause.'))
            for key, kind in PROXY_FIELDS:
                if o[key] != n[key]:
                    out.append(event('proxy',kind,ticker,before,after,key,o[key],n[key],
                        'Observed proxy/implementation indicator changed between sampled blocks. This is not proof of code execution, authorization, exploitation or investment risk.'))
    return out


def build(core, proxies, digests):
    events = supply_events(core) + proxy_events(proxies)
    events.sort(key=lambda x: (-x['to_block'], x['ticker'], x['scope'], x['kind'], x['event_id']))
    ids = [x['event_id'] for x in events]
    if len(ids) != len(set(ids)) or len(ids) > MAX_ALERTS:
        raise EvidenceError('DUPLICATE_OR_EXCESSIVE_EVENTS_MANUAL_REVIEW_REQUIRED')
    current_ambiguities = [item['ticker'] for item in proxies[-1]['assets']
        if item['pattern_observed'] == 'CONFLICTING_INDICATORS_REVIEW_REQUIRED']
    report = {'schema_version':SCHEMA,'chain_id':8453,
              'mode':'OFFLINE_VERIFIED_SAMPLE_COMPARISON_NO_TRADES',
              'source_sha256':digests,
              'base_observation_count':len(core),'proxy_observation_count':len(proxies),
              'base_latest_block':core[-1]['block']['number'],
              'proxy_latest_block':proxies[-1]['block']['number'],
              'proxy_currently_conflicting_indicators':current_ambiguities,
              'event_count':len(events), 'events':events,
              'disclaimer':('Changes are observed differences between verified sampled blocks, '
                            'not proof of the transaction, exact event block, intent, privilege, exploit, '
                            'investment safety, token rights, or airdrop eligibility. '
                            'Changes occurring and reversing between samples may be missed.')}
    if len(jbytes(report)) > MAX_JSON_BYTES:
        raise EvidenceError('ALERT_REPORT_TOO_LARGE_REQUIRES_MANUAL_REVIEW')
    return report


def pretty(x):
    if x is None:
        return 'gözlenmedi'
    if isinstance(x,dict) and 'units' in x:
        return f"{x['units']} token birimi (raw {x['raw']}; decimals {x['decimals']})"
    return str(x)


def render_section(report):
    esc = lambda v: html.escape(str(v),quote=True)
    count = report['event_count']
    latest = report['base_latest_block']
    ambiguities = report['proxy_currently_conflicting_indicators']
    header = (f'<section id="equity-alerts-v1" aria-labelledby="evidence-alerts-heading">'
              '<style id="equity-alerts-style-v1">'
              '#equity-alerts-v1{margin-top:36px;padding:22px;border:1px solid #355169;border-radius:16px;'
              'background:#101e2c;color:#e5f2fc}#equity-alerts-v1 h2{margin-top:0}'
              '#equity-alerts-v1 .ae-card{border:1px solid #395066;background:#192a3b;'
              'padding:14px;margin:12px 0;border-radius:12px;overflow-wrap:anywhere}'
              '#equity-alerts-v1 code{overflow-wrap:anywhere;font-size:12px}'
              '#equity-alerts-v1 a{color:#8ed8e9}'
              '#equity-alerts-v1 .ae-muted{color:#b5c6d5;font-size:13px}'
              '</style><h2 id="evidence-alerts-heading">Kanıta dayalı değişiklik incelemesi</h2>'
              '<p>Yalnızca kayıtlı Base blokları arasında gözlenen farklar. '
              'Uyarı bir güvenlik açığı, fiyat sinyali veya yatırım tavsiyesi değildir.</p>'
              f'<p><b>{count} gözlenmiş alan değişikliği</b> · '
              f'{report["base_observation_count"]} arz/kod gözlemi · '
              f'{report["proxy_observation_count"]} proxy gözlemi · '
              f'son Base bloğu #{latest}</p>')
    if ambiguities:
        header += (f'<p><strong>Manuel inceleme:</strong> Son örnekte çelişkili proxy '
                   f'göstergeleri var: {esc(", ".join(ambiguities))}. Bu, tek başına bir değişiklik alarmı değildir.</p>')
    rows = []
    for item in report['events'][:30]:
        before, after = item['from_block'], item['to_block']
        rows.append('<article class="ae-card">'
            f'<h3>{esc(item["ticker"])} · {esc(item["kind"].replace("_"," "))}</h3>'
            f'<p class="ae-muted">Gözlem aralığı: '
            f'<a href="https://basescan.org/block/{before}" rel="noopener noreferrer">#{before}</a> → '
            f'<a href="https://basescan.org/block/{after}" rel="noopener noreferrer">#{after}</a> · '
            f'Kanıt ID: <code>{esc(item["event_id"])}</code></p>'
            f'<p>Önce: <code>{esc(pretty(item["previous"]))}</code></p>'
            f'<p>Sonra: <code>{esc(pretty(item["current"]))}</code></p>'
            f'<p class="ae-muted">{esc(item["meaning"])}</p>'
            '</article>')
    if not rows:
        rows = ['<p>İzlenen alanlarda kayıtlı gözlemler arasında değişiklik bulunmadı. '
                'Bu sonuç, ara bloklarda hiç olay yaşanmadığını veya sözleşmelerin güvenli olduğunu kanıtlamaz.</p>']
    footer = ('<p class="ae-muted">Her olay için önceki/sonraki blok ve alan değerleri '
              'kayıtlıdır. Sıralı veriler birbirinden bağımsız risk olayları sayılmaz. '
              'Eksik desteklenen proxy türleri ve gözlemler arası olaylar kapsam dışıdır.</p>'
              '<p><a href="data/alerts.json">Bütün değişikliklerin JSON kanıtı</a> · '
              '<a href="data/alerts_manifest.json">Dosya bütünlüğü / kaynak özeti</a></p></section>')
    return header + ''.join(rows) + footer


def render_page(current, report):
    if current.count('</main>') != 1 or current.count('</head>') != 1:
        raise EvidenceError('INVALID_EXISTING_HTML_WRAPPER')
    if current.count(SECTION_MARKER) != 0:
        raise EvidenceError('EXISTING_ALERTS_SECTION_ALREADY_PRESENT_REBUILD_FROM_V4_OR_REMOVE_AFTER_CHECK')
    if current.count('id="proxy-evidence-v4"') != 1:
        raise EvidenceError('V4_PROXY_SECTION_NOT_READY')
    return current.replace('</main>',render_section(report)+'</main>',1).encode('utf-8')


def check_published(root, source_digests):
    a,b = (root/'data'/name for name in OUTPUT)
    found = [x.exists() for x in (a,b)]
    if not any(found):
        if SECTION_MARKER in (root/'index.html').read_text(encoding='utf-8'):
            raise EvidenceError('ORPHAN_ALERT_SECTION')
        return 'NOT_YET_PUBLISHED'
    if not all(found):
        raise EvidenceError('PARTIAL_ALERT_PUBLICATION')
    if not 0 < a.stat().st_size <= MAX_JSON_BYTES or not 0 < b.stat().st_size <= 100_000:
        raise EvidenceError('ALERT_FILE_SIZE_INVALID')
    try:
        report = json.loads(a.read_bytes()); manifest = json.loads(b.read_bytes())
    except ValueError as e:
        raise EvidenceError('INVALID_ALERT_JSON') from e
    if manifest.get('schema_version') != MANIFEST_SCHEMA or manifest.get('chain_id')!=8453:
        raise EvidenceError('INVALID_ALERT_MANIFEST_SCHEMA')
    digests = manifest.get('files_sha256')
    if not isinstance(digests,dict) or set(digests)!=set(SOURCE)|{'alerts.json'}:
        raise EvidenceError('INVALID_ALERT_MANIFEST_DIGEST_SET')
    for k in SOURCE:
        if digests[k]!=source_digests[k]:
            raise EvidenceError('STALE_ALERT_SOURCE_HASH:'+k)
    if digests['alerts.json']!=sha(a.read_bytes()):
        raise EvidenceError('ALERT_MANIFEST_HASH_MISMATCH')
    if report.get('schema_version')!=SCHEMA or report.get('chain_id')!=8453 or report.get('source_sha256')!=source_digests:
        raise EvidenceError('ALERT_DATA_SOURCE_INCONSISTENCY')
    if SECTION_MARKER not in (root/'index.html').read_text(encoding='utf-8'):
        raise EvidenceError('ALERT_PAGE_SECTION_MISSING')
    return 'CURRENT_VERIFIED'


def generate(root, *, check_only=False, preview=False):
    core, proxies, digests = inputs(root)
    status = check_published(root, digests) if check_only else 'BUILD'
    if check_only:
        return {'mode':'ALERT_OFFLINE_CHECK_ONLY','status':status,
                'base_observations':len(core),'proxy_observations':len(proxies),'written':False}
    report = build(core, proxies, digests)
    if preview:
        return {'mode':'ALERT_PREVIEW_NO_WRITE','event_count':report['event_count'],
                'kinds':sorted(set(e['kind'] for e in report['events'])), 'written':False}
    page = (root/'index.html').read_text(encoding='utf-8')
    if SECTION_MARKER in page:
        prior=check_published(root,digests)
        if prior=='CURRENT_VERIFIED':
            return {'mode':'ALERTS_ALREADY_CURRENT','event_count':report['event_count'],'written':False}
        raise EvidenceError('STALE_OR_INVALID_EXISTING_ALERT_SECTION')
    html_bytes = render_page(page,report)
    alerts = jbytes(report)
    manifest = {'schema_version':MANIFEST_SCHEMA,'chain_id':8453,
                'files_sha256':{**digests,'alerts.json':sha(alerts)},
                'note':'Only source-file consistency. Not cryptographic proof of EVM state.'}
    files={'index.html':html_bytes,'data/alerts.json':alerts,
           'data/alerts_manifest.json':jbytes(manifest)}
    with tempfile.TemporaryDirectory(prefix='equity-alerts-') as d:
        stage=Path(d)
        for rel,body in files.items():
            p=stage/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
        if json.loads((stage/'data/alerts_manifest.json').read_bytes())['files_sha256']['alerts.json'] != sha((stage/'data/alerts.json').read_bytes()):
            raise EvidenceError('STAGING_INTEGRITY_FAILURE')
        for rel in files:
            dst=root/rel;dst.parent.mkdir(parents=True,exist_ok=True)
            os.replace(stage/rel,dst)
    check_published(root,digests)
    return {'mode':'OFFLINE_CHANGE_REVIEW_WRITTEN',
            'event_count':report['event_count'],'base_observations':len(core),
            'proxy_observations':len(proxies),'latest_block':core[-1]['block']['number'],
            'written':True,'wallet_or_order_actions':'NONE'}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Offline, evidence-based Base sampled-change review')
    parser.add_argument('--site-root',type=Path,default=ROOT)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--check-only',action='store_true')
    group.add_argument('--preview',action='store_true')
    options=parser.parse_args(argv)
    try:
        out=generate(options.site_root,check_only=options.check_only,preview=options.preview)
        print(json.dumps(out,ensure_ascii=False,indent=2))
        return 0
    except (EvidenceError,ValueError,KeyError,TypeError,OverflowError,OSError) as ex:
        print(f'CHANGE_REVIEW_STOPPED_NO_PUBLISH: {type(ex).__name__}: {ex}',file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
