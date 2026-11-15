#!/usr/bin/env python3
"""Export two *real* Base Equity Lens V2 observations into a static public demo.

Read-only local transform. No HTTP, wallets, trading, keys, or deployment.
Does not change historical snapshots. Strictly refuses synthetic / incomplete data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import html
import json
from pathlib import Path
import re
import sys

from equity_lens import EvidenceError, compare, read_snapshot
from stock_integrity import ASSETS, BASE_ASSETS_SOURCE

ROOT = Path(__file__).resolve().parent
HEX64 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _date(value):
    if not isinstance(value, str):
        raise EvidenceError("Missing timestamp")
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("Bad timestamp") from exc
    if d.tzinfo is None:
        raise EvidenceError("Naive timestamp")
    return d.astimezone(timezone.utc)


def validate_live(snapshot):
    if snapshot.get("mode") != "LIVE_BASE_MAINNET_READ_ONLY":
        raise EvidenceError("PUBLIC_EXPORT_REFUSES_SYNTHETIC_OR_UNKNOWN_MODE")
    if snapshot.get("source_url") != BASE_ASSETS_SOURCE:
        raise EvidenceError("Unknown asset address reference")
    block = snapshot["block"]
    ts = _date(block.get("block_timestamp_utc"))
    captured = _date(snapshot.get("captured_at_utc"))
    if ts > captured or captured > datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=2):
        raise EvidenceError("Invalid captured/block time")
    if not isinstance(block.get("number"), int) or block["number"] < 1:
        raise EvidenceError("Bad block height")
    verified = {}
    for a in snapshot["assets"]:
        t = a["ticker"]
        if t in verified or t not in ASSETS or a.get("address", "").lower() != ASSETS[t].lower():
            raise EvidenceError("Unapproved or duplicate asset")
        if a.get("status") != "READ_OK":
            raise EvidenceError("REFUSES_INCOMPLETE_DATA")
        dig = a.get("code_sha256")
        if not isinstance(dig, str) or not HEX64.fullmatch(dig):
            raise EvidenceError("Bad runtime digest")
        raw = a.get("total_supply_raw")
        dec = a.get("decimals_reported")
        if not isinstance(raw, str) or not raw.isdecimal() or len(raw) > 90 or not isinstance(dec, int) or not 0 <= dec <= 36:
            raise EvidenceError("Bad supply data")
        size = a.get("code_bytes")
        if not isinstance(size, int) or size < 1:
            raise EvidenceError("Bad code size")
        units = str(int(raw) // 10 ** dec) if dec == 0 else None
        if dec:
            q, r = divmod(int(raw), 10 ** dec)
            units = f"{q}.{r:0{dec}d}".rstrip("0").rstrip(".")
        if a.get("token_units_reported") != units:
            raise EvidenceError("Supply unit calculation mismatch")
        verified[t] = {"ticker": t, "address": ASSETS[t], "status": "READ_OK",
                       "code_sha256": dig.lower(), "code_bytes": size, "total_supply_raw": raw,
                       "decimals_reported": dec, "token_units_reported": units}
    if set(verified) != set(ASSETS):
        raise EvidenceError("Missing assets")
    return {"schema_version": "equity-lens-public-snapshot-1", "chain_id": 8453,
            "mode": "LIVE_BASE_MAINNET_READ_ONLY", "captured_at_utc": captured.isoformat(),
            "block": {"number": block["number"], "hash": block["hash"].lower(),
                      "block_timestamp_utc": ts.isoformat()},
            "source_url": BASE_ASSETS_SOURCE,
            "assets": [verified[t] for t in ASSETS]}


def stable_json(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def public_page(old, new, report, manifest):
    e = html.escape
    rows = []
    for a in report["results"]:
        state = a["status"]
        flags = ", ".join(a["changes"]) if a["changes"] else "İncelenen alanlarda değişim yok"
        delta = a["raw_supply_pct_change"]
        old_a, new_a = a["old"], a["new"]
        rows.append(f'''<article class="asset"><div class="asset-head"><h3>{e(a["ticker"])}</h3><span class="tag">{e(state)}</span></div>
<p class="value">{e(str(new_a.get("token_units_reported")))}</p><p class="sub">Bildirilen token arzı · dolaşım arzı değil</p>
<dl><dt>Önceki arz</dt><dd>{e(str(old_a.get("token_units_reported")))}</dd>
<dt>Raw arz değişimi</dt><dd>{e(str(delta) + '%' if delta is not None else 'Bilinmiyor')}</dd>
<dt>İncelenen durum</dt><dd>{e(flags)}</dd>
<dt>Runtime kod SHA-256</dt><dd class="mono small">{e(str(new_a["code_sha256"]))}</dd>
<dt>Sözleşme</dt><dd class="mono small">{e(a["address"])}</dd></dl></article>''')
    oldblock, newblock = old["block"], new["block"]
    state = "İncelenen alanlarda değişiklik tespit edilmedi" if all(x["status"] == "NO_CHANGE_AT_SAMPLED_BLOCKS" for x in report["results"]) else "Değişiklikler incelenmeli"
    since = (datetime.now(timezone.utc) - _date(new["captured_at_utc"])).total_seconds() / 3600
    fresh_label = "Yakın zamanda alınmış kayıt" if since <= 24 else f"ESKİ KAYIT — {int(since)} saat önce"
    return f'''<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Base üzerindeki tokenlaştırılmış hisse sözleşmelerinin blok-sabitli, doğrulanabilir inceleme raporu.">
<title>Equity Evidence Lens | Base</title><style>
:root{{font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;color-scheme:dark;background:#081320;color:#eff6ff}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(135deg,#0d1b36,#081320 60%);min-height:100vh}}
main{{max-width:1130px;margin:auto;padding:36px 22px 70px}}.eyebrow{{color:#86b7ff;letter-spacing:.12em;text-transform:uppercase;font-size:12px;font-weight:800}}
h1{{font-size:clamp(36px,6vw,65px);line-height:1.06;letter-spacing:-.05em;max-width:850px;margin:16px 0}}
h2{{font-size:24px;margin:30px 0 12px}}p{{line-height:1.6;color:#b9cee5}}.lead{{font-size:18px;max-width:800px}}.banner{{border:1px solid #345777;background:#102943;padding:17px 19px;border-radius:14px;margin:22px 0}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}.stat,.asset{{background:#12243a;border:1px solid #294864;border-radius:17px;padding:19px}}
.stat strong{{display:block;font-size:26px;margin-top:5px;overflow-wrap:anywhere}}.stat span{{color:#a5bed7;font-size:12px}}.assets{{display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:14px}}
.asset-head{{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}}h3{{margin:0;font-size:25px}}.tag{{font-size:10px;border:1px solid #50718b;border-radius:30px;padding:6px 9px;color:#bcd9f5}}.value{{font-size:29px;font-weight:750;color:#fff;margin:14px 0 0;word-break:break-all}}
.sub{{font-size:12px;margin:2px 0 17px}}dl{{display:grid;grid-template-columns:1fr;gap:5px;margin:0}}dt{{color:#8eb1d2;font-size:11px;margin-top:8px}}dd{{margin:0;font-size:13px;overflow-wrap:anywhere}}.mono{{font-family:ui-monospace,SFMono-Regular,monospace}}.small{{font-size:11px}}a{{color:#8bc4ff}}.links{{display:flex;gap:10px;flex-wrap:wrap;margin-top:15px}}
.links a{{display:inline-block;border:1px solid #4173a6;border-radius:10px;padding:9px 12px;text-decoration:none;background:#12365b}}
.warn{{border-left:3px solid #e2b169;padding:10px 17px;background:#332a26;margin-top:25px;border-radius:8px}}
footer{{border-top:1px solid #2f435a;margin-top:30px;padding-top:15px;font-size:12px;color:#a5bed7}}
</style></head><body><main>
<div class="eyebrow">EVIDENCE-FIRST · BASE MAINNET · READ ONLY</div><h1>Tokenlaştırılmış hisseler için kanıt izi</h1>
<p class="lead">Üç Base sözleşmesindeki bildirilen arz, runtime kodu ve iki blok arasındaki gözlenebilir değişiklikler. Zincir üstü veriye dayanır; fiyat veya yatırım güvenliği derecelendirmez.</p>
<div class="banner"><strong>{e(state)}</strong><p>{e(fresh_label)} · Bu rapor sabit bir kayıttır, gerçek zamanlı ekran değildir. Herhangi bir tokenı al/sat önerisi veya ihraççı onayı içermez.</p></div>
<section class="stats"><div class="stat"><span>Ağ</span><strong>Base · 8453</strong></div>
<div class="stat"><span>İlk blok</span><strong>#{oldblock["number"]}</strong></div>
<div class="stat"><span>Son blok</span><strong>#{newblock["number"]}</strong></div>
<div class="stat"><span>Blok farkı</span><strong>{newblock["number"]-oldblock["number"]}</strong></div></section>
<h2>Varlık bazında bulgular</h2><section class="assets">{''.join(rows)}</section>
<h2>Denetlenebilir kaynaklar</h2><p>Her iki gözlem aynı blok içinde sabitlenmiş RPC okumalarından üretildi. Kayıtlar bağımsız olarak doğrulanmalı; RPC yanıtı kriptografik durum ispatı değildir.</p>
<div class="links"><a href="data/earlier.json">İlk kanıt JSON</a><a href="data/later.json">Son kanıt JSON</a><a href="data/comparison.json">Karşılaştırma JSON</a><a href="data/manifest.json">SHA-256 manifesti</a>
<a href="https://basescan.org/block/{newblock['number']}" target="_blank" rel="noopener noreferrer">Son bloku görüntüle</a><a href="{e(BASE_ASSETS_SOURCE)}" target="_blank" rel="noopener noreferrer">Resmî Base varlık listesi</a></div>
<div class="warn"><strong>Kapsam ve riskler</strong><p>Gözlenmeyen bloklar arasında değişiklik olmuş ve geri alınmış olabilir. Runtime kod değişmemesi proxy uygulamasının değişmediği anlamına gelmez. totalSupply() sonucu dolaşımdaki arz, ihraççı varlıkları, hisse hakları, fiyat, B20 ayarlaması veya likidite değildir. Kripto cüzdanı, işlem, yatırım ya da airdrop uygunluğu kontrolü yapılmaz.</p></div>
<footer>Son kayıt UTC: {e(new['captured_at_utc'])} · Son blok hash: <span class="mono small">{e(newblock['hash'])}</span>. Herhangi bir Base/Coinbase/ihraççı ortaklığı veya onayı iddia edilmez. Sayfa yalnızca bu dosyalardaki halka açık gözlemleri gösterir.</footer>
</main></body></html>'''


def build(snapshot_dir: Path, output_dir: Path):
    paths = sorted(snapshot_dir.glob("base_equity_*_b*.json"))
    if len(paths) < 2:
        raise EvidenceError("TWO_LIVE_SNAPSHOTS_REQUIRED")
    readings = [(p, read_snapshot(p)) for p in paths]
    # Chronological order by actual Base block, not filenames.
    readings.sort(key=lambda item: item[1]["block"]["number"])
    (_, older), (_, newer) = readings[-2:]
    old_pub, new_pub = validate_live(older), validate_live(newer)
    if old_pub["block"]["number"] >= new_pub["block"]["number"]:
        raise EvidenceError("TWO_DIFFERENT_BLOCKS_REQUIRED")
    if old_pub["block"]["hash"] == new_pub["block"]["hash"]:
        raise EvidenceError("Duplicate block hash")
    report = compare(older, newer)
    if any(x["status"] == "INSUFFICIENT_EVIDENCE" for x in report["results"]):
        raise EvidenceError("Comparison incomplete")
    # Only controlled public fields are exported. Never copy arbitrary user files or metadata.
    pub_results = [{"ticker": x["ticker"], "address": x["address"], "status": x["status"],
                    "changes": x["changes"], "raw_supply_delta": x["raw_supply_delta"],
                    "raw_supply_pct_change": x["raw_supply_pct_change"]} for x in report["results"]]
    comparison = {"schema_version": "equity-lens-public-comparison-1", "chain_id": 8453,
                  "earlier_block": old_pub["block"], "later_block": new_pub["block"],
                  "results": pub_results,
                  "interpretation": "Two observations only; not security certification or investment advice."}
    docs = {"earlier.json": stable_json(old_pub), "later.json": stable_json(new_pub),
            "comparison.json": stable_json(comparison)}
    manifest = {"schema_version": "equity-lens-public-manifest-1", "chain_id": 8453,
                "files_sha256": {name: hashlib.sha256(payload).hexdigest() for name, payload in docs.items()},
                "scope": "Sanitized, independent, publicly reproducible read-only Base observations. SHA-256 is file integrity, not chain state proof."}
    # Validate all input before touching output. Do not delete the original snapshot directory.
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / "data"
    dest.mkdir(parents=True, exist_ok=True)
    for name, content in docs.items():
        (dest / name).write_bytes(content)
    (dest / "manifest.json").write_bytes(stable_json(manifest))
    (output_dir / "index.html").write_text(public_page(old_pub, new_pub, report, manifest), encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return {"mode": "STATIC_PUBLIC_EXPORT_NO_DEPLOYMENT", "output": str(output_dir),
            "blocks": [old_pub["block"]["number"], new_pub["block"]["number"]],
            "statuses": {x["ticker"]: x["status"] for x in pub_results},
            "source_files_examined": len(paths), "exported_public_files": 6,
            "internet_upload": False, "wallet_or_order_actions": False}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Prepare static site from two real Base evidence snapshots. Never deploys or transacts.")
    ap.add_argument("--snapshots", default=str(ROOT / "snapshots"))
    ap.add_argument("--output", default=str(ROOT / "docs"))
    args = ap.parse_args(argv)
    try:
        out = build(Path(args.snapshots), Path(args.output))
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        print("PUBLIC_SITE_BUILD_STOPPED: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
