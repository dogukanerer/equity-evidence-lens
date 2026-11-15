#!/usr/bin/env python3
"""Base Equity Evidence Lens V2. Only allowlisted public read-only JSON-RPC.

Never provides investment advice; never signs, transfers, trades or connects wallets.
Snapshots are reproducible *observations*, not trustless Merkle/state proofs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import html
import json
from pathlib import Path
import re
import sys

from safe_rpc import RPCError, check_chain, parse_uint_hex, rpc
from stock_integrity import ASSETS, BASE_ASSETS_SOURCE

CHAIN_ID = 8453
UINT_RE = re.compile(r"0x[0-9a-fA-F]{64}\Z")
CODE_RE = re.compile(r"0x(?:[0-9a-fA-F]{2})+\Z")
BLOCK_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}\Z")
SELECTORS = {"total_supply_raw": "0x18160ddd", "decimals_reported": "0x313ce567"}
MAX_SNAPSHOT_BYTES = 200_000
OUTPUT_DIR = Path(__file__).resolve().parent


class EvidenceError(Exception):
    pass


def iso_utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def pinned_block(transport=None):
    """Get and validate one Base Mainnet height/hash, used as all calls' block tag."""
    if check_chain("base-mainnet", transport=transport) != CHAIN_ID:
        raise EvidenceError("Unexpected chain id")
    height = parse_uint_hex(rpc("base-mainnet", "eth_blockNumber", [], transport=transport))
    if height <= 0:
        raise EvidenceError("Invalid height")
    tag = hex(height)
    header = rpc("base-mainnet", "eth_getBlockByNumber", [tag, False], transport=transport)
    if not isinstance(header.get("hash"), str) or not BLOCK_HASH_RE.fullmatch(header["hash"]):
        raise EvidenceError("Missing/invalid block hash")
    if parse_uint_hex(header["number"]) != height:
        raise EvidenceError("Block number mismatch")
    timestamp = parse_uint_hex(header["timestamp"])
    if timestamp <= 0 or timestamp > int(datetime.now(timezone.utc).timestamp()) + 60:
        raise EvidenceError("Future/invalid block timestamp")
    return {"number": height, "hash": header["hash"].lower(),
            "block_timestamp_utc": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "block_tag": tag}


def read_uint(address, block_tag, selector, transport=None):
    raw = rpc("base-mainnet", "eth_call", [{"to": address, "data": selector}, block_tag], transport=transport)
    if not UINT_RE.fullmatch(raw):
        raise EvidenceError("Contract returned non-uint ABI")
    return int(raw, 16)


def read_asset(ticker, block_tag, transport=None):
    """Single contract, pinned block. Partial failures explicitly labelled."""
    if ticker not in ASSETS:
        raise EvidenceError("Unapproved asset")
    address = ASSETS[ticker]
    item = {"ticker": ticker, "address": address, "status": "NOT_VERIFIED",
            "code_sha256": None, "code_bytes": None, "total_supply_raw": None,
            "decimals_reported": None, "token_units_reported": None, "notes": []}
    try:
        code = rpc("base-mainnet", "eth_getCode", [address, block_tag], transport=transport)
        if code == "0x":
            item["status"] = "NO_CODE"
            item["notes"].append("No contract bytecode at this block")
            return item
        if not CODE_RE.fullmatch(code) or len(code) > 130_000:
            raise EvidenceError("Invalid or overlong code")
        bytecode = bytes.fromhex(code[2:])
        item["code_sha256"] = hashlib.sha256(bytecode).hexdigest()
        item["code_bytes"] = len(bytecode)
        # The digest is SHA-256 of returned runtime code, NOT EVM's keccak codehash.
        # Proxy implementation changes can occur without changing this digest.
        supply = read_uint(address, block_tag, SELECTORS["total_supply_raw"], transport)
        decimals = read_uint(address, block_tag, SELECTORS["decimals_reported"], transport)
        if decimals > 36:
            raise EvidenceError("Unreasonable decimals")
        item["total_supply_raw"] = str(supply)
        item["decimals_reported"] = decimals
        # Exact decimal formatting, no floating point or Decimal-context rounding.
        quotient, remainder = divmod(supply, 10 ** decimals)
        item["token_units_reported"] = (str(quotient) if decimals == 0 else
              f"{quotient}.{remainder:0{decimals}d}".rstrip("0").rstrip("."))
        item["status"] = "READ_OK"
    except (RPCError, ValueError, EvidenceError, TypeError, OverflowError) as exc:
        item["notes"].append(f"Read incomplete: {type(exc).__name__}")
    return item


def capture(transport=None, now=None):
    block = pinned_block(transport=transport)
    return {"schema_version": "equity-lens-2", "mode": "LIVE_BASE_MAINNET_READ_ONLY",
            "captured_at_utc": now or iso_utc(), "chain_id": CHAIN_ID,
            "source_url": BASE_ASSETS_SOURCE, "block": block,
            "assets": [read_asset(t, block["block_tag"], transport=transport) for t in ASSETS],
            "scope": "Three explicitly allowlisted Coinbase tokenized-stock contracts on Base Mainnet",
            "limitations": [
                "Pinned-height RPC observations are not cryptographic state proofs; provider honesty is assumed.",
                "A verified-assets reference is not endorsement, regulation approval or issuer-rights verification.",
                "Reported totalSupply is not circulating liquidity, token valuation or investment return.",
                "SHA-256 code digest does not cover proxy implementation upgrades, issuer/offchain rights or every upgrade pattern.",
                "Corporate-action B20 multipliers, prices, vesting, audits, prospectuses and jurisdiction eligibility are not verified.",
                "No wallet interaction, funds, order execution, signing or transaction broadcasting."
            ]}


def _get_assets(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "equity-lens-2":
        raise EvidenceError("Unsupported snapshot schema")
    if snapshot.get("chain_id") != CHAIN_ID or snapshot.get("mode") not in ("LIVE_BASE_MAINNET_READ_ONLY", "OFFLINE_DEMO_SYNTHETIC"):
        raise EvidenceError("Wrong chain or mode")
    block = snapshot.get("block")
    if not isinstance(block, dict) or not isinstance(block.get("number"), int) or block["number"] < 0:
        raise EvidenceError("Invalid block number")
    if not isinstance(block.get("hash"), str) or not BLOCK_HASH_RE.fullmatch(block["hash"]):
        raise EvidenceError("Invalid snapshot block hash")
    values = snapshot.get("assets")
    if not isinstance(values, list) or len(values) != len(ASSETS):
        raise EvidenceError("Incomplete asset list")
    result = {}
    for a in values:
        if not isinstance(a, dict) or a.get("ticker") not in ASSETS or a.get("address", "").lower() != ASSETS[a["ticker"]].lower() or a["ticker"] in result:
            raise EvidenceError("Ticker/address mismatch or duplicate")
        result[a["ticker"]] = a
    if set(result) != set(ASSETS):
        raise EvidenceError("Unapproved or missing asset")
    return result


def compare(previous, current):
    old = _get_assets(previous)
    new = _get_assets(current)
    if previous["mode"] != current["mode"]:
        raise EvidenceError("Synthetic and live snapshots cannot be compared")
    if current["block"]["number"] <= previous["block"]["number"]:
        raise EvidenceError("Current block must be strictly newer")
    outcomes = []
    for ticker in ASSETS:
        a,b = old[ticker], new[ticker]
        delta = None
        pct = None
        changes = []
        if a.get("status") == "READ_OK" and b.get("status") == "READ_OK":
            if a.get("code_sha256") != b.get("code_sha256"):
                changes.append("RUNTIME_CODE_SHA256_CHANGED")
            if a.get("decimals_reported") != b.get("decimals_reported"):
                changes.append("DECIMALS_CHANGED")
            try:
                prev, curr = int(a["total_supply_raw"]), int(b["total_supply_raw"])
                if min(prev,curr) < 0:
                    raise ValueError("Negative supply")
                delta = str(curr-prev)
                if prev > 0:
                    pct = format((Decimal(curr-prev)/Decimal(prev))*Decimal(100), ".6f")
                if curr != prev:
                    changes.append("RAW_TOTAL_SUPPLY_CHANGED")
            except (TypeError, ValueError, KeyError):
                changes.append("INVALID_SUPPLY_OBSERVATION")
            status = "CHANGE_REVIEW" if changes else "NO_CHANGE_AT_SAMPLED_BLOCKS"
        else:
            status = "INSUFFICIENT_EVIDENCE"
        outcomes.append({"ticker": ticker, "address": ASSETS[ticker],
                         "status": status, "changes": changes, "raw_supply_delta": delta,
                         "raw_supply_pct_change": pct, "old_status": a.get("status"),
                         "new_status": b.get("status"), "old": a, "new": b})
    return {"schema_version": "equity-lens-comparison-2", "mode": current["mode"],
            "generated_at_utc": iso_utc(), "source_url": BASE_ASSETS_SOURCE,
            "previous_block": previous["block"], "current_block": current["block"],
            "results": outcomes,
            "interpretation": "Changes are review triggers, NOT fraud findings; unchanged observations are NOT safety certifications.",
            "no_wallet_no_orders": True}


def read_snapshot(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise EvidenceError("Missing/oversized snapshot")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise EvidenceError("Invalid snapshot JSON") from exc
    _get_assets(data)
    return data


def save_json(path, data, exclusive=False):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with target.open("x" if exclusive else "w", encoding="utf-8") as f:
        f.write(encoded)
    return str(target)


def template_page(body, mode, title, subtitle, meta):
    esc = html.escape
    return f'''<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>
:root{{color-scheme:dark;font-family:Inter,system-ui,-apple-system,Arial;background:#08101c;color:#e8f0fa}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 90% 0,#133152 0,transparent 42%),#08101c;min-height:100vh}}
main{{max-width:1160px;margin:0 auto;padding:42px 24px 100px}}.nav{{display:flex;gap:9px;align-items:center;letter-spacing:.16em;text-transform:uppercase;font-size:11px;color:#99bde6;font-weight:800}}
.logo{{background:#2364f2;color:white;padding:9px 12px;border-radius:12px;letter-spacing:0}}h1{{font-size:clamp(31px,5vw,59px);letter-spacing:-.045em;margin:24px 0 12px;max-width:870px;line-height:1.08}}h2{{letter-spacing:-.025em;margin:26px 0 15px}}p{{line-height:1.62;color:#a5b8cc;max-width:930px}}.sub{{font-size:17px}}
.cols{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:15px;margin:22px 0}}.card{{border:1px solid #29415b;border-radius:18px;padding:21px;background:#122033;overflow-wrap:anywhere}}.card h3{{margin:0;font-size:23px}}.big{{font-weight:800;font-size:clamp(20px,2.5vw,30px);margin:12px 0}}.note{{font-size:12px;color:#a3bdd7}}.kv{{font-size:13px;color:#bfd2e8;line-height:1.95}}.mono{{font-family:ui-monospace,Consolas,monospace;word-break:break-all;font-size:12px}}.pill{{display:inline-block;margin:10px 0;border:1px solid #46627e;border-radius:100px;padding:5px 10px;color:#d9e9fa;font-size:11px;font-weight:700}}
.review{{border-color:#a47b34}}.unknown{{border-color:#955660}}.ok{{border-color:#236b65}}.banner{{margin:25px 0;padding:16px 18px;background:#1b2b40;border-left:3px solid #65a4f8;border-radius:8px;color:#d5e5f9}}
a{{color:#91c1ff}}.foot{{margin-top:30px;font-size:13px}}.muted{{color:#849ab2}}.flag{{color:#efc98b}}.risk{{color:#e8b0b0}}.line{{height:1px;background:#29415b;margin:26px 0}}code{{font-size:12px}}
</style></head><body><main><div class="nav"><span class="logo">E / L</span> BASE MAINNET · EVIDENCE LENS · V2</div>
<h1>{esc(title)}</h1><p class="sub">{esc(subtitle)}</p><div class="banner"><b>Çalışma modu:</b> {esc(mode)} · {esc(meta)}<br>Bu sayfa yatırım tavsiyesi, düzenleyici uygunluk, ihraççı güvencesi, fiyat kontrolü veya tokenın güvenli olduğuna ilişkin karar değildir.</div>
{body}<div class="line"></div><p class="foot">Adres karşılaştırma referansı: <a href="{esc(BASE_ASSETS_SOURCE)}" rel="noopener noreferrer" target="_blank">Base'in varlık listesi</a>. Token sahipliği, prospectus ve bölgesel erişim koşullarını ayrıca doğrulayın. Uygulama yalnızca halka açık RPC sorguları yapar; emir göndermez.</p></main></body></html>'''


def render_snapshot(snapshot):
    _get_assets(snapshot)
    esc = html.escape
    cards = []
    for a in snapshot["assets"]:
        good = a.get("status") == "READ_OK"
        tag = "ok" if good else "unknown"
        address = esc(str(a["address"]))
        cards.append(f'''<article class="card {tag}"><h3>{esc(a["ticker"])}</h3><span class="pill">{esc(a["status"])}</span>
<div class="big">{esc(str(a.get("token_units_reported") or "Doğrulanamadı"))}</div><div class="note">Bildirilen arz (token birimi) · piyasa likiditesi değil</div>
<div class="kv">Decimals: {esc(str(a.get("decimals_reported")))}<br>Runtime kod boyutu: {esc(str(a.get("code_bytes")))} bayt</div>
<div class="kv mono">Contract: {address}</div><div class="kv mono">Runtime SHA-256: {esc(str(a.get("code_sha256") or "—"))}</div>
</article>''')
    block = snapshot["block"]
    body = f'<h2>Sabitlenmiş blok kaydı</h2><p>Blok: <b>{block["number"]}</b> · UTC: {esc(block["block_timestamp_utc"])}<br><span class="mono">Block hash: {esc(block["hash"])}</span></p><section class="cols">{"".join(cards)}</section><p class="muted">SHA-256, JSON-RPC ile gelen runtime kodun yerel özetidir. Bir proxy arkasındaki uygulama kodu değişse bile bu özet aynı kalabilir.</p>'
    return template_page(body, snapshot["mode"], "Tokenlaştırılmış hisselerde kanıt izi", "Sabit bir Base bloğunda üç sözleşmenin arzı ve kodu hakkında tekrarlanabilir gözlemler.", snapshot["captured_at_utc"])


def render_comparison(report):
    esc=html.escape
    cards=[]
    for entry in report["results"]:
        status=entry["status"]
        css="review" if status=="CHANGE_REVIEW" else ("ok" if status=="NO_CHANGE_AT_SAMPLED_BLOCKS" else "unknown")
        flags=', '.join(entry["changes"]) if entry["changes"] else "Değişiklik göstergesi yok"
        delta=entry.get("raw_supply_pct_change")
        delta_s = (delta+'%') if delta is not None else 'Hesaplanamadı'
        cards.append(f'''<article class="card {css}"><h3>{esc(entry["ticker"])}</h3><span class="pill">{esc(status)}</span>
<div class="big">{esc(delta_s)}</div><div class="note">İki blok arasındaki raw totalSupply değişimi (yüzde)</div>
<div class="kv flag">{esc(flags)}</div><div class="kv">Önceki: {esc(str(entry["old"].get("token_units_reported") or "—"))}<br>Yeni: {esc(str(entry["new"].get("token_units_reported") or "—"))}</div>
<div class="kv mono">{esc(entry["address"])}</div></article>''')
    prev,curr=report["previous_block"], report["current_block"]
    body=f'<h2>İki blok arasındaki gözlenebilir değişiklikler</h2><p>Önce: <b>#{prev["number"]}</b> · Sonra: <b>#{curr["number"]}</b>. Bu bir süreklilik izleme sistemi değil; yalnızca iki anlık görüntüyü karşılaştırır.</p><section class="cols">{"".join(cards)}</section><div class="banner">Değişiklik bir dolandırıcılık bulgusu değildir. Değişiklik olmaması da güvenlik sertifikası değildir. Kurumsal işlemler/B20, likidite, fiyat ve kullanıcı hakları bu rapora dahil değildir.</div>'
    return template_page(body, report["mode"], "Neler değişti?", "Arz, decimals ve runtime bytecode değişimlerini inceleyen, emir göndermeyen Base izleme raporu.", report["generated_at_utc"])


def make_demo():
    def asset(ticker, units, code_hex):
        return {"ticker":ticker,"address":ASSETS[ticker],"status":"READ_OK", "code_sha256":code_hex*64,
                "code_bytes":10,"total_supply_raw":str(units*10**6),"decimals_reported":6,
                "token_units_reported":str(units),"notes":["SYNTHETIC, NOT ONCHAIN"]}
    block0={"number":100,"hash":"0x"+"a"*64,"block_timestamp_utc":"2026-11-15T09:00:00+00:00", "block_tag":"0x64"}
    block1={"number":200,"hash":"0x"+"b"*64,"block_timestamp_utc":"2026-11-15T10:00:00+00:00", "block_tag":"0xc8"}
    common={"schema_version":"equity-lens-2", "mode":"OFFLINE_DEMO_SYNTHETIC",
            "captured_at_utc":iso_utc(),"chain_id":CHAIN_ID,"source_url":BASE_ASSETS_SOURCE,
            "scope":"SYNTHETIC DATA - NO REAL PRICES OR STOCK CLAIMS"}
    first={**common, "block":block0, "assets":[asset("AAPLc",1000,"1"),asset("NVDAc",2000,"2"),asset("TSLAc",3000,"3")]}
    second={**common,"block":block1, "assets":[asset("AAPLc",1200,"1"),asset("NVDAc",2000,"2"),asset("TSLAc",3000,"4")]}
    return first,second


def main(argv=None):
    p=argparse.ArgumentParser(description="Evidence-first Base tokenized-stock monitoring; NO wallet or orders")
    sub=p.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Synthetic demo, no internet")
    sub.add_parser("capture", help="Live Base Mainnet read-only; stores one immutable observation")
    sub.add_parser("compare-latest", help="Compare most recent two LIVE saved snapshots")
    cp=sub.add_parser("compare", help="Compare two stored snapshots, strictly older then newer")
    cp.add_argument("previous")
    cp.add_argument("current")
    args=p.parse_args(argv)
    root=OUTPUT_DIR
    try:
        if args.command == "demo":
            prev,curr=make_demo()
            report=compare(prev,curr)
            save_json(root/"reports"/"demo_comparison.json",report)
            (root/"reports"/"demo_comparison.html").write_text(render_comparison(report),encoding="utf-8")
            print(json.dumps({"mode":"OFFLINE_DEMO_SYNTHETIC","report":"reports/demo_comparison.html",
                              "change_review_count":sum(x['status']=="CHANGE_REVIEW" for x in report['results'])},indent=2))
        elif args.command == "capture":
            snapshot=capture()
            stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            filepath=root/"snapshots"/f"base_equity_{stamp}_b{snapshot['block']['number']}.json"
            save_json(filepath,snapshot,exclusive=True)
            out=root/"reports"/"latest_snapshot.html"
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(render_snapshot(snapshot),encoding="utf-8")
            print(json.dumps({"mode":snapshot['mode'],"snapshot":str(filepath.relative_to(root)),
                             "report":str(out.relative_to(root)),
                             "block":snapshot["block"]["number"],
                             "statuses":{a["ticker"]:a["status"] for a in snapshot["assets"]}},indent=2))
            return 0 if all(a["status"]=="READ_OK" for a in snapshot["assets"]) else 2
        elif args.command in ("compare", "compare-latest"):
            if args.command == "compare-latest":
                files=sorted((root/"snapshots").glob("base_equity_*_b*.json"))
                if len(files) < 2:
                    raise EvidenceError("Need two live snapshots. Run 'capture' again after a later block.")
                prev,curr=read_snapshot(files[-2]),read_snapshot(files[-1])
            else:
                prev,curr=read_snapshot(args.previous),read_snapshot(args.current)
            report=compare(prev,curr)
            outfile=root/"reports"/"latest_comparison.html"
            save_json(root/"reports"/"latest_comparison.json",report)
            outfile.write_text(render_comparison(report),encoding="utf-8")
            print(json.dumps({"mode":report['mode'],"report":str(outfile.relative_to(root)),
                              "statuses":{r['ticker']:r['status'] for r in report['results']}},indent=2))
        return 0
    except (EvidenceError, RPCError, ValueError, TypeError, OSError, OverflowError) as exc:
        print(f"EQUITY_LENS_STOPPED: {type(exc).__name__}: {exc}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
