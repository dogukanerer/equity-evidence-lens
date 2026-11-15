#!/usr/bin/env python3
"""Base tokenized-stock contract checks and offline HTML artifact. READ-ONLY."""
import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from safe_rpc import check_chain, rpc, RPCError

# Contract addresses cross-checked against the official Base verified-stock list (short prefix/suffix)
# and full addresses published in the Base integration reference. Reconfirm before any use.
ASSETS = {
    "AAPLc": "0xb200000000000000000000C2e324d24d7eEcd1fb",
    "NVDAc": "0xb20000000000000000000078ee7ce2fE4908108C",
    "TSLAc": "0xb2000000000000000000001e800a7f5189430cD0",
}
BASE_ASSETS_SOURCE = "https://www.base.org/assets"
TOTAL_SUPPLY_SELECTOR = "0x18160ddd"
DECIMALS_SELECTOR = "0x313ce567"


def positive_uint_call(address, selector, transport=None):
    result = rpc("base-mainnet", "eth_call", [{"to":address,"data":selector},"latest"], transport=transport)
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", result):
        raise RPCError("Unexpected fixed-size uint return")
    return int(result,16)


def check_stock(ticker, transport=None):
    if ticker not in ASSETS:
        raise ValueError("Unrecognized stock asset")
    check_chain("base-mainnet", transport=transport)
    addr = ASSETS[ticker]
    code = rpc("base-mainnet", "eth_getCode", [addr, "latest"], transport=transport)
    if not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", code):
        raise RPCError("Malformed contract code")
    if code == "0x":
        return {"ticker":ticker,"address":addr,"status":"NO_CODE", "total_supply_raw":None,
                "claim":"No executable contract code was observed at this address on this RPC"}
    item = {"ticker":ticker,"address":addr,"status":"CODE_PRESENT_SUPPLY_UNVERIFIED", "total_supply_raw":None}
    try:
        supply = positive_uint_call(addr, TOTAL_SUPPLY_SELECTOR, transport=transport)
        item["total_supply_raw"] = str(supply)
        item["status"] = "ZERO_SUPPLY_REPORTED" if supply == 0 else "NONZERO_SUPPLY_REPORTED"
    except RPCError:
        item["status"] = "CODE_PRESENT_SUPPLY_UNVERIFIED"
    try:
        decimals = positive_uint_call(addr, DECIMALS_SELECTOR, transport=transport)
        if 0 <= decimals <= 36:
            item["decimals_reported"] = decimals
            if item["total_supply_raw"] is not None:
                item["token_units_reported"] = format(Decimal(item["total_supply_raw"]) / (Decimal(10) ** decimals), "f")
    except RPCError:
        pass
    item["claim"] = "Read-only contract observation only. Code, token supply and decimals do NOT prove tradeability, issuer endorsement, valuation, rights or eligibility. B20 corporate-action multipliers may affect exposure."
    return item


def build_report(results, demo=False):
    return {"mode":"OFFLINE_DEMO_SYNTHETIC" if demo else "LIVE_RPC_READ_ONLY",
            "generated_at_utc":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "official_verified_list":BASE_ASSETS_SOURCE, "scope":"Base Mainnet, approved contract addresses only",
            "assets":results,
            "limitations":["No prices, orderbooks, token rights, prospectuses, multiplier-adjusted holdings, ownership eligibility, or reward prediction.", "No wallet connection, signing, contract approval or transactions.", "Synthetic fixture results are not real onchain facts."]}


def dashboard(report):
    esc = html.escape
    labels = {"NONZERO_SUPPLY_REPORTED":"Arz bildirildi", "ZERO_SUPPLY_REPORTED":"Sıfır arz", "CODE_PRESENT_SUPPLY_UNVERIFIED":"Arz doğrulanamadı", "NO_CODE":"Sözleşme kodu yok", "NOT_VERIFIED":"Kontrol yapılamadı"}
    blocks=[]
    for asset in report["assets"]:
        status = asset.get("status", "NOT_VERIFIED")
        code=esc(str(asset.get("ticker","?")))
        desc=esc(labels.get(status,status))
        address=esc(asset.get("address", ""))
        supply=esc(str(asset.get("total_supply_raw") or "—"))
        unit=esc(str(asset.get("token_units_reported", "—")))
        blocks.append(f'<article class="tile"><div class="symbol">{code}</div><span class="tag">{desc}</span><div class="num">{unit}</div><div class="muted">Bildirilen token birimi (multiplier uygulanmamış)</div><div class="small"><b>Raw totalSupply</b> {supply}</div><div class="small mono">{address}</div></article>')
    mode=esc(report["mode"])
    timestamp=esc(report["generated_at_utc"])
    source=esc(report["official_verified_list"])
    cards='\n'.join(blocks)
    return f'''<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Base Stock Integrity Lab</title><style>
    :root{{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,Arial;background:#0a1019;color:#ebf5ff}}body{{margin:0;}}main{{max-width:1100px;margin:0 auto;padding:48px 22px}}h1{{font-size:clamp(30px,4.5vw,54px);margin:10px 0 9px}}p{{color:#a7b9cc;line-height:1.6;max-width:850px}}.eyebrow{{letter-spacing:.17em;color:#67d9bd;font-size:12px;text-transform:uppercase;font-weight:700}}.notice{{padding:15px 20px;background:#1b2838;border:1px solid #344a60;border-radius:12px;color:#dde9f8;margin:28px 0}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:15px}}.tile{{background:#111d2a;border:1px solid #2b4255;border-radius:18px;padding:23px;min-height:225px;overflow-wrap:anywhere}}.symbol{{font-size:27px;font-weight:800}}.tag{{display:inline-block;margin:12px 0;background:#184938;color:#9ff9d2;border-radius:40px;padding:6px 10px;font-size:12px}}.num{{font-size:28px;font-weight:700;margin:8px 0}}.muted{{color:#91a8bc;font-size:12px}}.small{{font-size:12px;color:#bed0e1;margin-top:15px}}.mono{{font-family:ui-monospace,monospace;color:#86a5be}}.foot{{margin-top:26px;font-size:12px}}a{{color:#8fdcc5}}.logo{{width:44px;height:44px;border-radius:14px;background:#1b55e8;display:grid;place-items:center;font-weight:900}}</style></head><body><main><div class="logo">B</div><div class="eyebrow">Evidence-first / read only</div><h1>Stock Integrity Lab</h1><p>Base üzerinde tokenlaştırılmış hisse sözleşmelerinin salt-okunur durum kontrolü. Giriş/satış emri vermez; token bulunması, onun herhangi bir ülkede alınıp satılabileceğini veya haklarını doğrulamaz.</p><div class="notice"><strong>Mod:</strong> {mode} &nbsp; · &nbsp; <strong>UTC:</strong> {timestamp}<br>Gerçek varlık değeri ve B20 kurumsal işlem çarpanı bu görünümde hesaplanmamaktadır.</div><section class="grid">{cards}</section><p class="foot">Kaynak listesini doğrula: <a href="{source}" target="_blank" rel="noopener noreferrer">Base verified assets</a>. Görüntü güncelliği raporun oluşturulma anına aittir. Sermaye ve yatırım kararı için kullanma.</p></main></body></html>'''


def main():
    ap=argparse.ArgumentParser(description="Base stock integrity lab, read-only")
    ap.add_argument("--demo", action="store_true", help="Uses entirely synthetic sample values")
    ap.add_argument("--html", default="reports/base_stock_integrity.html")
    ap.add_argument("--json", default="reports/base_stock_integrity.json")
    args=ap.parse_args()
    if args.demo:
        result=[{"ticker":ticker,"address":addr,"status":"NONZERO_SUPPLY_REPORTED","total_supply_raw":str(i*1000000000000),"token_units_reported":str(i*1000),"claim":"SYNTHETIC DEMO, NOT CHAIN DATA"} for i,(ticker,addr) in enumerate(ASSETS.items(),1)]
    else:
        result=[]
        for ticker,addr in ASSETS.items():
            try:
                result.append(check_stock(ticker))
            except (RPCError, ValueError) as e:
                result.append({"ticker":ticker,"address":addr,"status":"NOT_VERIFIED","error":str(e)})
    report=build_report(result, demo=args.demo)
    for out in (args.html,args.json):
        dest=Path(out)
        dest.parent.mkdir(parents=True, exist_ok=True)
    Path(args.html).write_text(dashboard(report), encoding="utf-8")
    Path(args.json).write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"mode":report["mode"],"assets":{x['ticker']:x['status'] for x in result},"json":args.json,"html":args.html},ensure_ascii=False,indent=2))
    return 0 if args.demo or all(x["status"]!="NOT_VERIFIED" for x in result) else 2

if __name__=="__main__":
    sys.exit(main())
