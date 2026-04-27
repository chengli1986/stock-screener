#!/usr/bin/env python3
"""East Money push2 API probe.

Dumps the full set of f1..f200 fields for 6 fixture stocks chosen to expose
the open questions in spec section 4 (sector tagging, HK fallback, ROE=0
sector-aware classification). Output goes to
tests/fixtures/eastmoney_probe/<symbol_norm>.json so future tests can replay.

Run: python3 scripts/probe_eastmoney.py
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "eastmoney_probe"

URL = "https://push2.eastmoney.com/api/qt/stock/get"
UT = "fa5fd1943c7b386f172d6893dbfba10b"
FIELDS = ",".join(f"f{i}" for i in range(1, 201))

FIXTURES = [
    # symbol_norm, name, market, rationale
    ("600900.SH", "长江电力",   "a",  "电力 — verify sector-aware ROE=0 review_needed"),
    ("600036.SH", "招商银行",   "a",  "银行 — verify gross_margin missing_expected for finance"),
    ("688235.SH", "百济神州",   "a",  "生物医药 pre-profit — second ROE=0 case"),
    ("600519.SH", "贵州茅台",   "a",  "消费白酒 — healthy A-share control"),
    ("0011.HK",   "恒生银行",   "hk", "HK 银行 — HK fallback case"),
    ("0700.HK",   "腾讯控股",   "hk", "HK 科技龙头 — healthy HK control"),
]


def em_secid(symbol_norm: str) -> str:
    if symbol_norm.endswith(".HK"):
        code = symbol_norm[:-3].zfill(5)
        return f"116.{code}"
    if symbol_norm.endswith(".SH"):
        return f"1.{symbol_norm[:-3]}"
    if symbol_norm.endswith(".SZ"):
        return f"0.{symbol_norm[:-3]}"
    raise ValueError(f"Unknown symbol format: {symbol_norm}")


def probe_one(symbol_norm: str) -> dict:
    secid = em_secid(symbol_norm)
    t0 = time.monotonic()
    r = requests.get(
        URL,
        params={"secid": secid, "fields": FIELDS, "ut": UT},
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    body = r.json() if r.status_code == 200 else None
    data = (body or {}).get("data") if isinstance(body, dict) else None
    return {
        "secid": secid,
        "http_status": r.status_code,
        "elapsed_ms": elapsed_ms,
        "field_count": len(data) if isinstance(data, dict) else 0,
        "data": data,
    }


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Probing {len(FIXTURES)} fixtures, fields=f1..f200")
    print(f"Output dir: {FIXTURE_DIR}")
    print()

    summary = []
    for symbol_norm, name, market, rationale in FIXTURES:
        try:
            result = probe_one(symbol_norm)
        except Exception as e:
            print(f"  ❌ {symbol_norm} ({name}) — {type(e).__name__}: {e}")
            summary.append({"symbol": symbol_norm, "ok": False, "error": str(e)})
            continue

        record = {
            "symbol_norm": symbol_norm,
            "name": name,
            "market": market,
            "rationale": rationale,
            "probe": result,
        }
        out_path = FIXTURE_DIR / f"{symbol_norm}.json"
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))

        d = result.get("data") or {}
        f127 = d.get("f127", "<absent>")
        f128 = d.get("f128", "<absent>")
        print(f"  ✅ {symbol_norm:11s} {name:6s} ({market}) — {result['field_count']} fields, "
              f"f127={f127!r}, f128={f128!r}, {result['elapsed_ms']}ms")
        summary.append({
            "symbol": symbol_norm,
            "ok": True,
            "field_count": result["field_count"],
            "f127": f127,
            "f128": f128,
        })

    print()
    print(f"Wrote {len(summary)} fixture files to {FIXTURE_DIR}")
    return 0 if all(s.get("ok") for s in summary) else 1


if __name__ == "__main__":
    sys.exit(main())
