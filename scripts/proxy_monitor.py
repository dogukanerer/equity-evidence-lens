#!/usr/bin/env python3
"""V4: ERC-1967 and canonical ERC-1167 proxy *observations*, not a security audit.
Uses the newest independently validated V3 block; all RPC calls are read-only.
No signatures, personal data, trading, approvals or transactions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / 'engine'))
from equity_lens import EvidenceError
from safe_rpc import RPCError, check_chain, rpc, parse_uint_hex
from stock_integrity import ASSETS
from scripts.refresh_site import history_from_site, utc

SCHEMA = 'equity-lens-proxy-observation-1'
HISTORY_SCHEMA = 'equity-lens-proxy-history-1'
MANIFEST_SCHEMA = 'equity-lens-proxy-manifest-1'
ERC1967_SLOTS = {
    'implementation': '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc',
    'admin': '0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103',
    'beacon': '0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50',
}
BEACON_IMPL_SELECTOR = '0x5c60da1b'
CLONE_PREFIX = '363d3d373d3d3d363d73'
CLONE_SUFFIX = '5af43d82803e903d91602b57fd5bf3'
HEX_WORD = re.compile(r'0x[0-9a-fA-F]{64}\Z')
HEX_CODE = re.compile(r'0x(?:[0-9a-fA-F]{2})+\Z')
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}\Z')
ZERO_WORD = '0x' + '0' * 64
MAX_HISTORY_BYTES = 3_000_000
MAX_RECORDS = 730
MAX_CODE_HEX = 130_000


def safe_code(raw):
    if not isinstance(raw, str) or not HEX_CODE.fullmatch(raw) or len(raw) > MAX_CODE_HEX:
        raise EvidenceError('INVALID_OR_MISSING_RUNTIME_CODE')
    code = bytes.fromhex(raw[2:])
    return {'sha256': hashlib.sha256(code).hexdigest(), 'bytes':len(code)}


def parse_word(raw):
    if not isinstance(raw, str) or not HEX_WORD.fullmatch(raw):
        raise EvidenceError('BAD_ERC1967_STORAGE_WORD')
    return raw.lower()


def word_address(raw):
    raw = parse_word(raw)
    if raw == ZERO_WORD:
        return None
    if int(raw[2:26], 16) != 0:
        raise EvidenceError('ERC1967_NON_ADDRESS_STORAGE_VALUE')
    address = '0x' + raw[-40:]
    if int(address, 16) == 0:
        return None
    return address


def clone_target(raw):
    """Only the exact 45-byte ERC-1167 canonical stub; other variants UNCLASSIFIED."""
    if not isinstance(raw, str) or not HEX_CODE.fullmatch(raw):
        raise EvidenceError('INVALID_CLONE_BYTECODE')
    code = raw[2:].lower()
    if len(code) == 90 and code.startswith(CLONE_PREFIX) and code.endswith(CLONE_SUFFIX):
        addr = '0x' + code[len(CLONE_PREFIX):len(CLONE_PREFIX)+40]
        if int(addr, 16) == 0:
            raise EvidenceError('INVALID_ZERO_CLONE_TARGET')
        return addr
    return None


def capture_asset(ticker, block_tag, prior, transport=None):
    address = ASSETS[ticker]
    runtime = rpc('base-mainnet', 'eth_getCode', [address, block_tag], transport=transport)
    runtime_info = safe_code(runtime)
    if (runtime_info['sha256'] != prior['code_sha256'] or
            runtime_info['bytes'] != prior['code_bytes']):
        raise EvidenceError('RUNTIME_CODE_MISMATCH_WITH_VERIFIED_V3_SNAPSHOT:' + ticker)
    words = {key:parse_word(rpc('base-mainnet','eth_getStorageAt',
                [address, slot, block_tag], transport=transport))
             for key, slot in ERC1967_SLOTS.items()}
    addresses = {key:word_address(raw) for key,raw in words.items()}
    eip1167 = clone_target(runtime)
    impl = addresses['implementation']
    beacon = addresses['beacon']
    target = None
    target_source = None
    indicators = int(impl is not None) + int(beacon is not None) + int(eip1167 is not None)
    if indicators > 1:
        classification = 'CONFLICTING_INDICATORS_REVIEW_REQUIRED'
    elif impl is not None:
        target, target_source = impl, 'ERC1967_IMPLEMENTATION_SLOT'
        classification = 'ERC1967_IMPLEMENTATION_OBSERVED'
    elif beacon is not None:
        safe_code(rpc('base-mainnet','eth_getCode',[beacon,block_tag],transport=transport))
        raw = rpc('base-mainnet','eth_call',[
            {'to':beacon,'data':BEACON_IMPL_SELECTOR},block_tag],transport=transport)
        target = word_address(raw)
        if target is None:
            raise EvidenceError('BEACON_RETURNED_ZERO_IMPLEMENTATION')
        target_source = 'ERC1967_BEACON_IMPLEMENTATION_CALL'
        classification = 'ERC1967_BEACON_OBSERVED'
    elif eip1167 is not None:
        target, target_source = eip1167, 'ERC1167_CANONICAL_BYTECODE'
        classification = 'CANONICAL_ERC1167_CLONE_OBSERVED'
    else:
        classification = 'UNCLASSIFIED_STANDARD_SLOTS_EMPTY'
    target_info = (safe_code(rpc('base-mainnet','eth_getCode',
                   [target,block_tag],transport=transport)) if target else None)
    return {
        'ticker':ticker, 'address':address, 'runtime_code_sha256':runtime_info['sha256'],
        'pattern_observed':classification,
        'eip1967_storage_words':words, 'eip1967_addresses':addresses,
        'erc1167_canonical_target':eip1167,
        'implementation_target_address':target,
        'target_source':target_source,
        'target_runtime_sha256':target_info['sha256'] if target_info else None,
        'target_runtime_bytes':target_info['bytes'] if target_info else None,
        'authorization_analysis':'NOT_PERFORMED',
    }


def collect(latest, *, transport=None, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise EvidenceError('NAIVE_CURRENT_TIMESTAMP')
    observed = utc(latest['captured_at_utc'])
    age = (now.astimezone(timezone.utc) - observed).total_seconds()
    if age < -120 or age > 1800:
        raise EvidenceError('V3_SNAPSHOT_TOO_OLD_REFRESH_FIRST')
    check_chain('base-mainnet', transport=transport)
    block = latest['block']
    tag = hex(block['number'])
    header = rpc('base-mainnet','eth_getBlockByNumber',[tag,False],transport=transport)
    if (parse_uint_hex(header['number']) != block['number'] or
            not HEX_WORD.fullmatch(header['hash']) or
            header['hash'].lower() != block['hash'].lower()):
        raise EvidenceError('V3_BLOCK_HEADER_HASH_MISMATCH')
    by_ticker={x['ticker']:x for x in latest['assets']}
    assets=[capture_asset(t,tag,by_ticker[t],transport=transport) for t in ASSETS]
    header2 = rpc('base-mainnet','eth_getBlockByNumber',[tag,False],transport=transport)
    if header2['hash'].lower()!=block['hash'].lower() or parse_uint_hex(header2['number'])!=block['number']:
        raise EvidenceError('V3_BLOCK_HASH_CHANGED_DURING_PROXY_READS')
    result={'schema_version':SCHEMA,'chain_id':8453,
        'mode':'LIVE_BASE_MAINNET_READ_ONLY',
        'collected_at_utc':now.astimezone(timezone.utc).isoformat(),
        'block':block, 'assets':assets,
        'limitations':[
          'ERC-1967 slots and exact ERC-1167 runtime cover ONLY a subset of proxy architectures.',
          'Empty admin slot does NOT prove absent upgrade or owner powers.',
          'Implementation code hash is a SHA-256 of provider-reported bytes, not proof of secure code.',
          'This does NOT analyze UUPS owner roles, access control, timelocks, upgrades between sampled blocks or issuer rights.',
          'RPC observations are not cryptographic state proofs or investment advice.'
        ]}
    validate_observation(result)
    return result


def validate_observation(record):
    if not isinstance(record,dict) or record.get('schema_version')!=SCHEMA or record.get('chain_id')!=8453 or record.get('mode')!='LIVE_BASE_MAINNET_READ_ONLY':
        raise EvidenceError('BAD_PROXY_RECORD_SCHEMA')
    block=record.get('block')
    if not isinstance(block,dict) or not isinstance(block.get('number'),int) or isinstance(block.get('number'),bool) or block['number']<1 or not HEX_WORD.fullmatch(block.get('hash','')):
        raise EvidenceError('BAD_PROXY_BLOCK_ID')
    utc(record.get('collected_at_utc'))
    values=record.get('assets')
    if not isinstance(values,list) or len(values)!=len(ASSETS) or set(x.get('ticker') for x in values if isinstance(x,dict))!=set(ASSETS):
        raise EvidenceError('BAD_PROXY_ASSET_COLLECTION')
    for item in values:
        if item.get('address','').lower()!=ASSETS[item['ticker']].lower():
            raise EvidenceError('PROXY_TICKER_ADDRESS_MISMATCH')
        if item.get('authorization_analysis')!='NOT_PERFORMED':
            raise EvidenceError('UNSUPPORTED_AUTHORITY_CLAIM')
        if not isinstance(item.get('runtime_code_sha256'),str) or not re.fullmatch('[0-9a-f]{64}',item['runtime_code_sha256']):
            raise EvidenceError('BAD_PROXY_RUNTIME_HASH')
        words=item.get('eip1967_storage_words')
        if not isinstance(words,dict) or set(words)!=set(ERC1967_SLOTS):
            raise EvidenceError('BAD_PROXY_SLOT_SET')
        for v in words.values(): parse_word(v)
        addresses=item.get('eip1967_addresses')
        if not isinstance(addresses,dict) or addresses!={k:word_address(v) for k,v in words.items()}:
            raise EvidenceError('INCONSISTENT_PROXY_ADDRESSES')
        for field in ('implementation_target_address','erc1167_canonical_target'):
            val=item.get(field)
            if val is not None and (not isinstance(val,str) or not ADDRESS.fullmatch(val)):
                raise EvidenceError('BAD_PROXY_TARGET')
        dig=item.get('target_runtime_sha256')
        size=item.get('target_runtime_bytes')
        if (dig is None) != (size is None):
            raise EvidenceError('INCOMPLETE_TARGET_RUNTIME')
        if dig is not None and (not isinstance(dig,str) or not re.fullmatch('[0-9a-f]{64}',dig) or not isinstance(size,int) or size < 1):
            raise EvidenceError('BAD_TARGET_RUNTIME')
        if item.get('implementation_target_address') is None and dig is not None:
            raise EvidenceError('UNLINKED_TARGET_CODE_HASH')
        expected_pattern = ('CONFLICTING_INDICATORS_REVIEW_REQUIRED'
            if sum(x is not None for x in (addresses['implementation'], addresses['beacon'], item.get('erc1167_canonical_target'))) > 1
            else 'ERC1967_IMPLEMENTATION_OBSERVED' if addresses['implementation'] is not None
            else 'ERC1967_BEACON_OBSERVED' if addresses['beacon'] is not None
            else 'CANONICAL_ERC1167_CLONE_OBSERVED' if item.get('erc1167_canonical_target') is not None
            else 'UNCLASSIFIED_STANDARD_SLOTS_EMPTY')
        if item.get('pattern_observed') != expected_pattern:
            raise EvidenceError('INCONSISTENT_PROXY_CLASSIFICATION')
        expected_source = {'ERC1967_IMPLEMENTATION_OBSERVED':'ERC1967_IMPLEMENTATION_SLOT',
            'ERC1967_BEACON_OBSERVED':'ERC1967_BEACON_IMPLEMENTATION_CALL',
            'CANONICAL_ERC1167_CLONE_OBSERVED':'ERC1167_CANONICAL_BYTECODE'}.get(expected_pattern)
        if item.get('target_source') != expected_source:
            raise EvidenceError('INCONSISTENT_TARGET_SOURCE')
        expected_target = (addresses['implementation'] if expected_pattern=='ERC1967_IMPLEMENTATION_OBSERVED'
            else item.get('erc1167_canonical_target') if expected_pattern=='CANONICAL_ERC1167_CLONE_OBSERVED'
            else None)
        if expected_pattern in ('ERC1967_IMPLEMENTATION_OBSERVED','CANONICAL_ERC1167_CLONE_OBSERVED') and item.get('implementation_target_address') != expected_target:
            raise EvidenceError('INCONSISTENT_IMPLEMENTATION_TARGET')
        if expected_source is None and item.get('implementation_target_address') is not None:
            raise EvidenceError('UNEXPECTED_TARGET_WITHOUT_VERIFIED_SOURCE')
        if expected_source is not None and item.get('implementation_target_address') is None:
            raise EvidenceError('MISSING_VERIFIED_IMPLEMENTATION')
        if expected_source is not None and dig is None:
            raise EvidenceError('MISSING_TARGET_CODE_HASH')
    return record


def load_json(path,max_bytes=MAX_HISTORY_BYTES):
    if not path.is_file() or path.stat().st_size>max_bytes:
        raise EvidenceError('MISSING_OR_OVERSIZE_PROXY_FILE')
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,UnicodeError) as exc: raise EvidenceError('CORRUPT_PROXY_JSON') from exc


def read_prior(root, main_series):
    names=['proxy_history.json','proxy_audit.json','proxy_manifest.json']
    paths=[root/'data'/n for n in names]
    found=[p.exists() for p in paths]
    if not any(found): return []
    if not all(found): raise EvidenceError('PARTIAL_PROXY_PUBLICATION')
    m=load_json(paths[2],100_000)
    if m.get('schema_version')!=MANIFEST_SCHEMA or m.get('chain_id')!=8453:
        raise EvidenceError('INVALID_PROXY_MANIFEST')
    hashes=m.get('files_sha256')
    if not isinstance(hashes,dict) or set(hashes)!=set(names[:2]):
        raise EvidenceError('INVALID_PROXY_MANIFEST_CONTENT')
    for n,digest in hashes.items():
        f = root / ('data/'+n)
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
            raise EvidenceError('INVALID_PROXY_MANIFEST_DIGEST')
        if not f.is_file() or f.stat().st_size>MAX_HISTORY_BYTES or hashlib.sha256(f.read_bytes()).hexdigest()!=digest:
            raise EvidenceError('PROXY_MANIFEST_DIGEST_MISMATCH:'+n)
    history=load_json(paths[0]); last=load_json(paths[1]);
    if history.get('schema_version')!=HISTORY_SCHEMA or history.get('chain_id')!=8453:
        raise EvidenceError('INVALID_PROXY_HISTORY_SCHEMA')
    series=history.get('observations')
    if not isinstance(series,list) or not 1 <= len(series) <= MAX_RECORDS:
        raise EvidenceError('INVALID_PROXY_HISTORY_LENGTH')
    for item in series: validate_observation(item)
    if series[-1]!=last: raise EvidenceError('PROXY_HISTORY_TIP_MISMATCH')
    if any(series[i]['block']['number']>=series[i+1]['block']['number'] for i in range(len(series)-1)):
        raise EvidenceError('PROXY_HEIGHT_NOT_MONOTONIC')
    if series[-1]['block']['number']>main_series[-1]['block']['number']:
        raise EvidenceError('PROXY_AUDIT_AHEAD_OF_MAIN_HISTORY')
    if (series[-1]['block']['number']==main_series[-1]['block']['number']
            and 'id="proxy-evidence-v4"' not in (root/'index.html').read_text(encoding='utf-8')):
        raise EvidenceError('PROXY_PAGE_SECTION_MISSING_FOR_CURRENT_AUDIT')
    block_map={x['block']['number']:x['block']['hash'] for x in main_series}
    for item in series:
        if block_map.get(item['block']['number'])!=item['block']['hash']:
            raise EvidenceError('PROXY_HISTORY_BLOCK_NOT_IN_MAIN_HISTORY')
    return series


def review_changes(before,after):
    if before is None: return {ticker:['BASELINE_ONLY'] for ticker in ASSETS}
    old={x['ticker']:x for x in before['assets']}
    new={x['ticker']:x for x in after['assets']}
    result={}
    for ticker in ASSETS:
        keys=('eip1967_storage_words','implementation_target_address','target_runtime_sha256',
              'pattern_observed','erc1167_canonical_target')
        changes=[k for k in keys if old[ticker].get(k)!=new[ticker].get(k)]
        result[ticker]=changes or ['NO_CHANGE_AT_SAMPLED_BLOCKS']
    return result


def render_section(latest,changes,count):
    e=html.escape
    cards=[]
    for item in latest['assets']:
        ticker=item['ticker']; target=item['implementation_target_address']
        admin=item['eip1967_addresses']['admin']
        changes_here=changes[ticker]
        attention=changes_here!=['NO_CHANGE_AT_SAMPLED_BLOCKS'] and changes_here!=['BASELINE_ONLY']
        status=('İnceleme gerektiren değişiklik' if attention else
                'İlk gözlem' if changes_here==['BASELINE_ONLY'] else 'Örneklenen alanlarda değişiklik yok')
        cards.append('<article class="proxy-card">'
                     f'<h3>{e(ticker)}</h3><p><b>Durum:</b> {e(status)}</p>'
                     f'<p><b>Gözlenen yapı:</b> <code>{e(item["pattern_observed"])}</code></p>'
                     f'<p><b>Hedef uygulama:</b> <code>{e(target or "TESPİT EDİLEMEDİ")}</code></p>'
                     f'<p><b>Hedef kod SHA-256:</b> <code>{e(item["target_runtime_sha256"] or "—")}</code></p>'
                     f'<p><b>ERC-1967 admin slotu:</b> <code>{e(admin or "BOŞ / GÖZLENMEDİ")}</code></p>'
                     f'<p><b>Değişim işaretleri:</b> {e(", ".join(changes_here))}</p></article>')
    return ('<section id="proxy-evidence-v4" class="proxy-evidence">'
            '<h2>Proxy ve uygulama kanıtları</h2>'
            '<p>ERC-1967 standart depolama alanları ve yalnızca tam ERC-1167 minimal proxy kodu kontrol edilir. '
            'Boş slot, yükseltme veya yönetim yetkisinin bulunmadığını kanıtlamaz. '
            'UUPS yetkileri, roller, timelock ve özel proxy türleri denetlenmemiştir.</p>'
            f'<p>Son denetlenen Base bloğu: <b>#{latest["block"]["number"]}</b> · {count} gözlem · '
            'Sabit örnekler; ara bloklarda gerçekleşen olaylar bilinmiyor.</p>'
            '<div class="proxy-grid">'+''.join(cards)+'</div>'
            '<p><a href="data/proxy_audit.json">Son proxy kanıtı (JSON)</a> · '
            '<a href="data/proxy_history.json">Geçmiş proxy kanıtları (JSON)</a> · '
            '<a href="data/proxy_manifest.json">Dosya SHA-256 manifesti</a></p>'
            '<p>RPC tarafından raporlanan veridir; kaynak doğrulama, kod güvenliği, yatırım uygunluğu veya airdrop hakkı kanıtı değildir.</p>'
            '</section>')


def build_artifacts(root,series,latest):
    changes=review_changes(series[-1] if series else None,latest)
    expanded = (series if series and series[-1]['block']['number']==latest['block']['number'] else series+[latest])
    history={'schema_version':HISTORY_SCHEMA,'chain_id':8453,'observations':expanded,
             'note':'Sampled proxy-related state; not continuous, not a security audit.'}
    doc={'data/proxy_audit.json':latest,'data/proxy_history.json':history}
    raw={name:(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+'\n').encode()
         for name,value in doc.items()}
    page=(root/'index.html').read_text(encoding='utf-8')
    if 'id="proxy-evidence-v4"' in page or page.count('</main>')!=1:
        raise EvidenceError('UNEXPECTED_PREEXISTING_PROXY_MARKUP_OR_INVALID_HTML')
    css=('<style>.proxy-evidence{margin-top:34px}.proxy-grid{display:grid;'
         'grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}'
         '.proxy-card{padding:18px;border:1px solid #2e4c65;background:#14263b;border-radius:16px}'
         '.proxy-card code{font-size:11px;overflow-wrap:anywhere}'
         '.proxy-evidence p{overflow-wrap:anywhere}</style>')
    if page.count('</head>')!=1: raise EvidenceError('INVALID_PAGE_HEAD')
    page=page.replace('</head>',css+'</head>').replace('</main>',render_section(latest,changes,len(expanded))+'</main>')
    raw['index.html']=page.encode('utf-8')
    manifest={'schema_version':MANIFEST_SCHEMA,'chain_id':8453,
              'files_sha256':{k.removeprefix('data/'):hashlib.sha256(v).hexdigest() for k,v in raw.items() if k!='index.html'},
              'note':'Integrity of public files only; NOT an EVM state proof.'}
    raw['data/proxy_manifest.json']=(json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True)+'\n').encode()
    return raw


def write_artifacts(root,contents):
    """Prepare all files before replacing. Workflow publishes ONLY after full success."""
    with tempfile.TemporaryDirectory(prefix='equity-proxy-') as d:
        temp=Path(d)
        for name, body in contents.items():
            if len(body)>MAX_HISTORY_BYTES: raise EvidenceError('OUTPUT_TOO_LARGE')
            p=temp/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
        m=json.loads((temp/'data/proxy_manifest.json').read_bytes())
        for name,digest in m['files_sha256'].items():
            target=temp/('data/'+name)
            if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
                raise EvidenceError('STAGED_HASH_MISMATCH')
        for name in contents:
            target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
            os.replace(temp/name,target)


def refresh_proxy(root,*,collector=collect,check_only=False):
    core=history_from_site(root)
    old=read_prior(root,core)
    if check_only:
        return {'mode':'PROXY_LOCAL_VALIDATION_NO_NETWORK', 'main_history':len(core),
                'proxy_history':len(old), 'written':False}
    latest=core[-1]
    # Do not emit an unauthenticated new audit if V3 auto-refresh did not happen recently.
    result=collector(latest)
    validate_observation(result)
    if result['block']['number']!=latest['block']['number'] or result['block']['hash']!=latest['block']['hash']:
        raise EvidenceError('PROXY_AUDIT_NOT_PINNED_TO_LATEST_V3_BLOCK')
    if old and result['block']['number']<old[-1]['block']['number']:
        raise EvidenceError('PROXY_AUDIT_REWIND')
    if old and result['block']['number']==old[-1]['block']['number']:
        if result['assets'] != old[-1]['assets']:
            raise EvidenceError('SAME_BLOCK_PROXY_STATE_CONFLICT')
        if 'id="proxy-evidence-v4"' in (root/'index.html').read_text(encoding='utf-8'):
            return {'mode':'PROXY_AUDIT_ALREADY_CURRENT','block':result['block']['number'],
                    'history_samples':len(old), 'written':False}
        # V3 refresh regenerated the HTML; reconstruct audited section without duplication.
        result=old[-1]
        old=old[:-1]
    if len(old)+1>MAX_RECORDS:
        raise EvidenceError('PROXY_HISTORY_CAPACITY_REQUIRES_MANUAL_REVIEW')
    contents=build_artifacts(root,old,result)
    write_artifacts(root,contents)
    return {'mode':'VERIFIED_PROXY_OBSERVATIONS_NO_TRADES',
            'block':result['block']['number'], 'history_samples':len(old)+1,
            'patterns':{x['ticker']:x['pattern_observed'] for x in result['assets']},
            'wallet_or_order_actions':'NONE', 'written':True}


def main(argv=None):
    p=argparse.ArgumentParser(description='Pinned EIP1967/1167 monitor; public read-only Base RPC')
    p.add_argument('--site-root',type=Path,default=BASE)
    p.add_argument('--check-only',action='store_true',help='Validate previous evidence; no network or changes')
    args=p.parse_args(argv)
    try:
        out=refresh_proxy(args.site_root,check_only=args.check_only)
        print(json.dumps(out,indent=2,ensure_ascii=False))
        return 0
    except (EvidenceError,RPCError,KeyError,TypeError,ValueError,OverflowError,OSError) as exc:
        print(f'PROXY_AUDIT_STOPPED_NO_PUBLISH: {type(exc).__name__}: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
