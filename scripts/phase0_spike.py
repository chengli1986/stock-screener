#!/usr/bin/env python3
"""
Phase 0 Data Spike — answers: what data can we get, what can't we get, why, how long.

Pipeline (run in order):
  1. Universe  → artifacts/phase0/universe.csv       (§A 8-field schema)
  2. OHLCV     → artifacts/phase0/ohlcv.csv          (Longbridge kline)
  3. Fundamentals → artifacts/phase0/fundamentals.jsonl  (East Money push2, §B tri-state)
  4. Report    → artifacts/phase0/report.json + coverage_report.md + timing.csv

Usage:
  python3 phase0_spike.py --limit 15              # dry run (fixed samples per §C)
  python3 phase0_spike.py --limit 70              # small batch
  python3 phase0_spike.py                         # full run (~900)
  python3 phase0_spike.py --market a --limit 50   # A-share only
  python3 phase0_spike.py --workers 4             # parallel OHLCV
  python3 phase0_spike.py --force                 # ignore resume, refetch all
  python3 phase0_spike.py --skip-ohlcv            # fundamentals only
  python3 phase0_spike.py --skip-fundamentals     # OHLCV only

Phase 0 invariant (§G): CSI 300 + CSI 500 only; ChiNext names may appear
only as CSI 500 constituents, not as a separately sourced universe.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

# ── paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_DIR = os.path.join(REPO_DIR, "config")
ARTIFACTS_DIR = os.path.join(REPO_DIR, "artifacts", "phase0")

HK_CONSTITUENTS_FILE = os.path.join(CONFIG_DIR, "hk_constituents.json")
UNIVERSE_CSV = os.path.join(ARTIFACTS_DIR, "universe.csv")
OHLCV_CSV = os.path.join(ARTIFACTS_DIR, "ohlcv.csv")
FUNDAMENTALS_JSONL = os.path.join(ARTIFACTS_DIR, "fundamentals.jsonl")
REPORT_JSON = os.path.join(ARTIFACTS_DIR, "report.json")
COVERAGE_MD = os.path.join(ARTIFACTS_DIR, "coverage_report.md")
TIMING_CSV = os.path.join(ARTIFACTS_DIR, "timing.csv")

# Fixed dry-run sample (15 stocks, frozen per §C + §G guardrail 2)
DRY_RUN_SYMBOLS = [
    # A-share — SSE (5)
    "600519.SH",  # 贵州茅台 consumer
    "600036.SH",  # 招商银行 financials
    "601318.SH",  # 中国平安 financials
    "600276.SH",  # 恒瑞医药 healthcare
    "600900.SH",  # 长江电力 utilities
    # A-share — SZSE main-board (3)
    "000333.SZ",  # 美的集团 consumer/manufacturing
    "000858.SZ",  # 五粮液 consumer
    "000651.SZ",  # 格力电器 consumer/manufacturing
    # A-share — ChiNext (2), both verified CSI 500 constituents per §G
    "300454.SZ",  # 深信服 tech
    "300450.SZ",  # 先导智能 manufacturing
    # HK (5)
    "0700.HK",    # 腾讯
    "0005.HK",    # 汇丰
    "0941.HK",    # 中移动
    "9988.HK",    # 阿里
    "3690.HK",    # 美团
]

BJT = timezone(timedelta(hours=8))

# East Money push2 field mapping (battle-tested in fetch_stock_data.py)
# A-share secid prefix: 1 = SSE, 0 = SZSE
# HK secid prefix: 116
EM_FIELDS = "f116,f162,f163,f167,f173,f183,f184,f185,f186,f187"
EM_FIELD_MAP = {
    "f116": "market_cap",       # 总市值 (元)
    "f162": "_pe_static_raw",   # PE静态 ÷100
    "f163": "_pe_dynamic_raw",  # PE动态 ÷100
    "f167": "_pb_raw",          # PB ÷100
    "f173": "roe_ttm",          # ROE % (already %)
    "f183": "_revenue_raw",     # 营收TTM (元, for cross-check only)
    "f184": "revenue_growth",   # 营收同比 %
    "f185": "net_profit_growth", # 净利同比 %
    "f186": "gross_margin",     # 毛利率 %
    "f187": "net_margin_ttm",   # 净利率 %
}

# HK fields known to return 0.0 (not null) when data is unavailable
# These are classified as missing_expected for HK, NOT fetch_error (§B)
# net_profit_growth added after dry-run confirmed HK returns 0.0 for it too
# (§I pre-run assumed available; dry run disagreed → updated here per §I update rule)
HK_MISSING_EXPECTED_FIELDS = {"revenue_growth", "net_margin_ttm", "gross_margin", "net_profit_growth"}

# A-share fields where null is structurally expected for certain sectors
# gross_margin: financial sector (banks/insurance/brokers) has no gross margin concept
# — East Money returns null/0 for these, not a data error
A_SHARE_STRUCTURAL_MISSING = {"gross_margin"}

# Fundamentals fields in canonical order (frozen per §I)
FUNDAMENTALS_FIELDS = [
    "roe_ttm",
    "revenue_growth",
    "net_profit_growth",
    "net_margin_ttm",
    "gross_margin",
    "pe_ttm",
    "pb",
    "market_cap",
]


# ── error classification ──────────────────────────────────────────────────────

def classify_error(exc: Exception | None, returncode: int | None = None,
                   stderr: str = "") -> str:
    """Map an exception or subprocess result to a canonical error_type."""
    if returncode is not None and returncode != 0:
        if "timeout" in stderr.lower():
            return "timeout"
        if "429" in stderr or "rate" in stderr.lower():
            return "rate_limited"
        return "subprocess_error"
    if exc is None:
        return "unknown"
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "429" in msg or "rate limit" in msg or "connection reset" in msg:
        return "rate_limited"
    if "symbol" in msg and ("not found" in msg or "unknown" in msg):
        return "symbol_not_found"
    if "connection" in msg or "network" in msg or "connect" in msg:
        return "connection_error"
    if "json" in msg or "parse" in msg or "decode" in msg:
        return "parse_error"
    return "unknown"


# ── symbol normalization ──────────────────────────────────────────────────────

def normalize_a_share(raw_code: str, exchange: str = "") -> tuple[str, str]:
    """
    Returns (symbol_raw, symbol_norm) for an A-share code.
    raw_code: as returned by akshare (6-digit, no suffix)
    exchange: exchange name from akshare (e.g. '上交所', '深交所')
    """
    code = raw_code.strip().lstrip("0") or "0"
    code = raw_code.strip()  # keep leading zeros, e.g. '000333'
    if exchange and "上" in exchange:
        suffix = "SH"
    elif exchange and "深" in exchange:
        suffix = "SZ"
    else:
        # Derive from code prefix
        if code.startswith("6"):
            suffix = "SH"
        else:
            suffix = "SZ"
    return code, f"{code}.{suffix}"


def em_secid(symbol_norm: str) -> str:
    """Convert symbol_norm to East Money secid format."""
    if symbol_norm.endswith(".HK"):
        code = symbol_norm[:-3].zfill(5)
        return f"116.{code}"
    if symbol_norm.endswith(".SH"):
        return f"1.{symbol_norm[:-3]}"
    if symbol_norm.endswith(".SZ"):
        return f"0.{symbol_norm[:-3]}"
    raise ValueError(f"Unknown symbol format: {symbol_norm}")


# ── output helpers ────────────────────────────────────────────────────────────

UNIVERSE_FIELDS = [
    "market", "symbol_raw", "symbol_norm", "name",
    "universe_source", "source_status", "last_verified", "source_note",
]

OHLCV_FIELDS = [
    "symbol_norm", "market", "rows", "time_s",
    "fetch_status", "error_type", "error_msg",
]

TIMING_FIELDS = [
    "phase", "total_stocks", "succeeded", "failed", "elapsed_s", "avg_per_stock_s",
]


def _write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _append_jsonl(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_done_symbols(path: str, key: str = "symbol_norm") -> set[str]:
    """Load symbols that already have fetch_status == ok (resume support, §E)."""
    done: set[str] = set()
    if not os.path.isfile(path):
        return done
    if path.endswith(".csv"):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("fetch_status") == "ok":
                    done.add(row[key])
    elif path.endswith(".jsonl"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("fetch_status") == "ok":
                        done.add(r[key])
                except json.JSONDecodeError:
                    pass
    return done


# ── step 1: universe generation ───────────────────────────────────────────────

def build_universe(market_filter: str | None, limit: int | None,
                   dry_run: bool) -> list[dict]:
    """
    Generate the unified universe (§A 8-field schema).

    A-share: akshare CSI 300 + CSI 500, deduplicated (CSI 300 wins on conflict).
    HK: static config/hk_constituents.json (provisional seed).

    Returns list of row dicts matching UNIVERSE_FIELDS.
    """
    import akshare as ak

    rows: list[dict] = []
    now_bjt = datetime.now(BJT).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    if market_filter != "hk":
        print("[universe] Fetching CSI 300 from akshare...", flush=True)
        df300 = ak.index_stock_cons_csindex("000300")
        print(f"[universe] Fetching CSI 500 from akshare...", flush=True)
        df500 = ak.index_stock_cons_csindex("000905")

        seen: set[str] = set()
        for df, source in [(df300, "csi300"), (df500, "csi500")]:
            for _, r in df.iterrows():
                raw = str(r["成分券代码"]).zfill(6)
                exchange = str(r.get("交易所", ""))
                name = str(r.get("成分券名称", ""))
                _, norm = normalize_a_share(raw, exchange)
                if norm in seen:
                    continue  # CSI 300 already added this stock
                seen.add(norm)
                rows.append({
                    "market": "a",
                    "symbol_raw": raw,
                    "symbol_norm": norm,
                    "name": name,
                    "universe_source": source,
                    "source_status": "live",
                    "last_verified": now_bjt,
                    "source_note": "akshare index_stock_cons_csindex runtime snapshot",
                })

    if market_filter != "a":
        print(f"[universe] Loading HK seed from {HK_CONSTITUENTS_FILE}...", flush=True)
        with open(HK_CONSTITUENTS_FILE, encoding="utf-8") as f:
            hk_data = json.load(f)
        for c in hk_data["constituents"]:
            rows.append({
                "market": c["market"],
                "symbol_raw": c["symbol_raw"],
                "symbol_norm": c["symbol_norm"],
                "name": c["name"],
                "universe_source": c["universe_source"],
                "source_status": c["source_status"],
                "last_verified": c["last_verified"],
                "source_note": c["source_note"],
            })

    # Apply dry-run fixed sample filter (§C guardrail 2)
    if dry_run:
        target = set(DRY_RUN_SYMBOLS)
        rows = [r for r in rows if r["symbol_norm"] in target]
        # Preserve DRY_RUN_SYMBOLS order for reproducibility
        order = {s: i for i, s in enumerate(DRY_RUN_SYMBOLS)}
        rows.sort(key=lambda r: order.get(r["symbol_norm"], 999))

    # Apply market filter + limit (non-dry-run)
    if not dry_run:
        if market_filter == "a":
            rows = [r for r in rows if r["market"] == "a"]
        elif market_filter == "hk":
            rows = [r for r in rows if r["market"] == "hk"]
        if limit:
            rows = rows[:limit]

    return rows


# ── step 2: OHLCV fetching ─────────────────────────────────────────────────────

def fetch_ohlcv_one(symbol_norm: str, market: str) -> dict:
    """Fetch 60-day daily kline via Longbridge CLI. Returns ohlcv row dict."""
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["longbridge", "kline", symbol_norm, "--period", "day", "--count", "60"],
            capture_output=True, text=True, timeout=30,
        )
        elapsed = time.monotonic() - t0
        if result.returncode != 0:
            return {
                "symbol_norm": symbol_norm, "market": market,
                "rows": 0, "time_s": round(elapsed, 2),
                "fetch_status": "error",
                "error_type": classify_error(None, result.returncode, result.stderr),
                "error_msg": result.stderr.strip()[:200],
            }
        # Count data rows (skip header + separator lines)
        lines = [l for l in result.stdout.splitlines()
                 if l.strip() and not l.startswith("|---") and not l.startswith("| Time")]
        row_count = len(lines)
        return {
            "symbol_norm": symbol_norm, "market": market,
            "rows": row_count, "time_s": round(elapsed, 2),
            "fetch_status": "ok", "error_type": "", "error_msg": "",
        }
    except subprocess.TimeoutExpired:
        return {
            "symbol_norm": symbol_norm, "market": market,
            "rows": 0, "time_s": round(time.monotonic() - t0, 2),
            "fetch_status": "error", "error_type": "timeout", "error_msg": "subprocess timeout",
        }
    except Exception as exc:
        return {
            "symbol_norm": symbol_norm, "market": market,
            "rows": 0, "time_s": round(time.monotonic() - t0, 2),
            "fetch_status": "error",
            "error_type": classify_error(exc),
            "error_msg": str(exc)[:200],
        }


def run_ohlcv(universe: list[dict], force: bool, workers: int) -> list[dict]:
    """Run OHLCV phase. Returns list of result rows."""
    import concurrent.futures

    done = set() if force else _load_done_symbols(OHLCV_CSV)
    todo = [r for r in universe if r["symbol_norm"] not in done]
    if not todo:
        print("[ohlcv] All symbols already done (resume). Use --force to refetch.")
        with open(OHLCV_CSV, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    print(f"[ohlcv] Fetching {len(todo)} symbols ({len(done)} already done)...", flush=True)
    results: list[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_ohlcv_one, r["symbol_norm"], r["market"]): r["symbol_norm"]
            for r in todo
        }
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            sym = futures[fut]
            row = fut.result()
            results.append(row)
            status = "✓" if row["fetch_status"] == "ok" else f"✗ {row['error_type']}"
            print(f"[ohlcv] {i}/{len(todo)} {sym} {status} ({row['rows']} rows, {row['time_s']}s)",
                  flush=True)

    # Merge with existing (resume): load existing ok rows + new results
    existing_ok = []
    if not force and os.path.isfile(OHLCV_CSV):
        with open(OHLCV_CSV, newline="", encoding="utf-8") as f:
            existing_ok = [r for r in csv.DictReader(f) if r.get("fetch_status") == "ok"]
    all_rows = existing_ok + results
    _write_csv(OHLCV_CSV, OHLCV_FIELDS, all_rows)
    return all_rows


# ── step 3: fundamentals fetching ─────────────────────────────────────────────

def fetch_fundamentals_one(symbol_norm: str, market: str) -> dict:
    """
    Fetch fundamentals via East Money push2. Returns fundamentals record.
    Uses §B tri-state: available / missing_expected / fetch_error.

    NOTE (Phase 0/1 diagnostic-precision tradeoff): when the API returns
    0.0 for a field that should have a value, this is classified the same
    as a failed API call — both map to fetch_error (or missing_expected for
    known-absent HK fields). A null return and a failed API call are NOT the
    same thing, but the counts don't yet justify a second state. A future phase
    splitting these (e.g. fetch_error_null vs fetch_error_api) should find this
    comment and treat it as the branching point.
    """
    import requests

    t0 = time.monotonic()
    secid = em_secid(symbol_norm)
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": EM_FIELDS,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }

    base = {
        "symbol_norm": symbol_norm,
        "market": market,
        "fetch_status": "error",
        "error_type": "",
        "error_msg": "",
        "time_s": 0.0,
    }

    try:
        r = requests.get(url, params=params, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        elapsed = round(time.monotonic() - t0, 2)
        base["time_s"] = elapsed

        if r.status_code == 429:
            base["error_type"] = "rate_limited"
            base["error_msg"] = "HTTP 429"
            return base

        if r.status_code != 200:
            base["error_type"] = "connection_error"
            base["error_msg"] = f"HTTP {r.status_code}"
            return base

        data = r.json().get("data")
        if not data:
            base["error_type"] = "empty_response"
            base["error_msg"] = "data field null/missing"
            return base

        # Extract raw values
        raw_pe_s = data.get("f162") or 0
        raw_pe_d = data.get("f163") or 0
        raw_pb = data.get("f167") or 0

        pe_ttm = round(raw_pe_s / 100, 2) if raw_pe_s else (
            round(raw_pe_d / 100, 2) if raw_pe_d else None)
        pb = round(raw_pb / 100, 2) if raw_pb else None
        market_cap = data.get("f116") or None
        roe_ttm = data.get("f173") or None
        revenue_growth = data.get("f184") or None
        net_profit_growth = data.get("f185") or None
        gross_margin = data.get("f186") or None
        net_margin_ttm = data.get("f187") or None

        def field_status(field_name: str, value) -> str:
            """
            Assign tri-state status for a fundamentals field.
            For HK stocks, known-absent fields (0.0 return) = missing_expected.
            For A-share, gross_margin null = missing_expected (financial sector
            has no gross margin concept; East Money returns null for banks/insurance).
            Other A-share 0.0 = fetch_error. See fetch_error_null note above.
            """
            if value is not None and value != 0:
                return "available"
            if market == "hk" and field_name in HK_MISSING_EXPECTED_FIELDS:
                return "missing_expected"
            if market == "a" and field_name in A_SHARE_STRUCTURAL_MISSING:
                return "missing_expected"
            return "fetch_error"

        record = {
            "symbol_norm": symbol_norm,
            "market": market,
            "fetch_status": "ok",
            "error_type": "",
            "error_msg": "",
            "time_s": elapsed,
            "roe_ttm":          {"value": roe_ttm,          "field_status": field_status("roe_ttm", roe_ttm)},
            "revenue_growth":   {"value": revenue_growth,   "field_status": field_status("revenue_growth", revenue_growth)},
            "net_profit_growth":{"value": net_profit_growth,"field_status": field_status("net_profit_growth", net_profit_growth)},
            "net_margin_ttm":   {"value": net_margin_ttm,   "field_status": field_status("net_margin_ttm", net_margin_ttm)},
            "gross_margin":     {"value": gross_margin,     "field_status": field_status("gross_margin", gross_margin)},
            "pe_ttm":           {"value": pe_ttm,           "field_status": field_status("pe_ttm", pe_ttm)},
            "pb":               {"value": pb,               "field_status": field_status("pb", pb)},
            "market_cap":       {"value": market_cap,       "field_status": field_status("market_cap", market_cap)},
        }
        return record

    except requests.Timeout:
        base["time_s"] = round(time.monotonic() - t0, 2)
        base["error_type"] = "timeout"
        base["error_msg"] = "requests timeout"
        return base
    except Exception as exc:
        base["time_s"] = round(time.monotonic() - t0, 2)
        base["error_type"] = classify_error(exc)
        base["error_msg"] = str(exc)[:200]
        return base


def run_fundamentals(universe: list[dict], force: bool) -> list[dict]:
    """Run fundamentals phase sequentially (East Money rate-limit sensitive)."""
    done = set() if force else _load_done_symbols(FUNDAMENTALS_JSONL)
    todo = [r for r in universe if r["symbol_norm"] not in done]
    if not todo:
        print("[fundamentals] All symbols already done (resume). Use --force to refetch.")
        records = []
        with open(FUNDAMENTALS_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    print(f"[fundamentals] Fetching {len(todo)} symbols ({len(done)} already done)...", flush=True)
    results: list[dict] = []

    for i, row in enumerate(todo, 1):
        rec = fetch_fundamentals_one(row["symbol_norm"], row["market"])
        _append_jsonl(FUNDAMENTALS_JSONL, rec)
        results.append(rec)
        status = "✓" if rec["fetch_status"] == "ok" else f"✗ {rec['error_type']}"
        print(f"[fundamentals] {i}/{len(todo)} {row['symbol_norm']} {status} ({rec['time_s']}s)",
              flush=True)
        if i % 10 == 0:
            time.sleep(0.5)  # gentle rate-limit pause every 10 calls

    return results


# ── step 4: report generation ──────────────────────────────────────────────────

def build_report(universe: list[dict], ohlcv_rows: list[dict],
                 fund_records: list[dict], timings: list[dict]) -> None:
    """Generate report.json + coverage_report.md + timing.csv (§D fixed metrics)."""
    # --- timing.csv ---
    _write_csv(TIMING_CSV, TIMING_FIELDS, timings)

    # --- universe totals ---
    total = len(universe)
    by_market = {}
    by_source = {}
    for r in universe:
        by_market[r["market"]] = by_market.get(r["market"], 0) + 1
        by_source[r["universe_source"]] = by_source.get(r["universe_source"], 0) + 1
    hk_null_last_verified = sum(1 for r in universe if r["market"] == "hk"
                                and not r.get("last_verified"))

    # --- OHLCV success ---
    ohlcv_by_market: dict[str, dict] = {}
    for r in ohlcv_rows:
        m = r.get("market", "?")
        if m not in ohlcv_by_market:
            ohlcv_by_market[m] = {"total": 0, "ok": 0}
        ohlcv_by_market[m]["total"] += 1
        if r.get("fetch_status") == "ok":
            ohlcv_by_market[m]["ok"] += 1

    # --- fundamentals coverage ---
    fund_by_market: dict[str, dict] = {}
    for rec in fund_records:
        m = rec.get("market", "?")
        if m not in fund_by_market:
            fund_by_market[m] = {"total": 0, "ok": 0, "fields": {}}
        fund_by_market[m]["total"] += 1
        if rec.get("fetch_status") == "ok":
            fund_by_market[m]["ok"] += 1
            for fname in FUNDAMENTALS_FIELDS:
                fd = rec.get(fname, {})
                fs = fd.get("field_status", "fetch_error") if isinstance(fd, dict) else "fetch_error"
                fstats = fund_by_market[m]["fields"].setdefault(fname, {"available": 0, "missing_expected": 0, "fetch_error": 0})
                fstats[fs] = fstats.get(fs, 0) + 1

    # --- error counts ---
    error_counts: dict[str, int] = {}
    for r in ohlcv_rows:
        if r.get("fetch_status") != "ok":
            et = r.get("error_type", "unknown")
            error_counts[et] = error_counts.get(et, 0) + 1
    for rec in fund_records:
        if rec.get("fetch_status") != "ok":
            et = rec.get("error_type", "unknown")
            k = f"fund_{et}"
            error_counts[k] = error_counts.get(k, 0) + 1

    # --- sample detail (15 dry-run rows) ---
    sample_symbols = set(DRY_RUN_SYMBOLS)
    sample_fund = {r["symbol_norm"]: r for r in fund_records
                   if r["symbol_norm"] in sample_symbols}

    report = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "universe": {
            "total": total,
            "by_market": by_market,
            "by_source": by_source,
            "hk_null_last_verified": hk_null_last_verified,
        },
        "ohlcv": ohlcv_by_market,
        "fundamentals": fund_by_market,
        "error_counts": error_counts,
        "sample_detail": sample_fund,
    }

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # --- coverage_report.md ---
    lines = [
        "# Phase 0 Coverage Report",
        f"\nGenerated: {report['generated_at']}",
        "\n> **WARNING**: HK constituents are provisional seeds — not verified against",
        "> official HSI/HSCEI constituent announcements. Do not use for production.",
        "\n## Universe",
        f"- Total: {total}",
    ]
    for mkt, cnt in by_market.items():
        lines.append(f"  - {mkt}: {cnt}")
    for src, cnt in by_source.items():
        lines.append(f"  - source={src}: {cnt}")
    if hk_null_last_verified:
        lines.append(f"- HK rows with null last_verified: {hk_null_last_verified} (expected — provisional seed)")

    lines.append("\n## OHLCV")
    for mkt, stats in ohlcv_by_market.items():
        pct = stats["ok"] / stats["total"] * 100 if stats["total"] else 0
        lines.append(f"- {mkt}: {stats['ok']}/{stats['total']} ({pct:.1f}%)")

    lines.append("\n## Fundamentals Coverage")
    for mkt, stats in fund_by_market.items():
        lines.append(f"\n### {mkt} ({stats['ok']}/{stats['total']} stocks OK)")
        lines.append("| Field | available | missing_expected | fetch_error |")
        lines.append("|-------|-----------|-----------------|-------------|")
        for fname in FUNDAMENTALS_FIELDS:
            fs = stats["fields"].get(fname, {})
            lines.append(
                f"| {fname} | {fs.get('available',0)} | {fs.get('missing_expected',0)} | {fs.get('fetch_error',0)} |"
            )

    lines.append("\n## Error Counts")
    for et, cnt in sorted(error_counts.items()):
        lines.append(f"- {et}: {cnt}")

    lines.append("\n## Timing")
    for t in timings:
        lines.append(f"- {t['phase']}: {t['elapsed_s']}s total, {t['avg_per_stock_s']}s/stock")

    with open(COVERAGE_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[report] Written: {REPORT_JSON}")
    print(f"[report] Written: {COVERAGE_MD}")
    print(f"[report] Written: {TIMING_CSV}")


# ── main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 0 data spike")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--market", choices=["a", "hk"], default=None)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--force", action="store_true")
    p.add_argument("--skip-ohlcv", action="store_true")
    p.add_argument("--skip-fundamentals", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.limit == 15 and args.market is None
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)

    timings: list[dict] = []

    # ── step 1: universe ──
    t0 = time.monotonic()
    print(f"\n=== Step 1: Universe {'(dry run)' if dry_run else ''} ===", flush=True)
    universe = build_universe(args.market, args.limit, dry_run)
    _write_csv(UNIVERSE_CSV, UNIVERSE_FIELDS, universe)
    elapsed = round(time.monotonic() - t0, 2)
    a_count = sum(1 for r in universe if r["market"] == "a")
    hk_count = sum(1 for r in universe if r["market"] == "hk")
    print(f"[universe] {len(universe)} stocks (A:{a_count} HK:{hk_count}) → {UNIVERSE_CSV}")
    timings.append({"phase": "universe", "total_stocks": len(universe), "succeeded": len(universe),
                    "failed": 0, "elapsed_s": elapsed, "avg_per_stock_s": 0})

    if len(universe) == 0:
        print("[universe] ERROR: empty universe — check API or config", file=sys.stderr)
        sys.exit(1)

    # ── step 2: OHLCV ──
    ohlcv_rows: list[dict] = []
    if not args.skip_ohlcv:
        t0 = time.monotonic()
        print(f"\n=== Step 2: OHLCV ({args.workers} workers) ===", flush=True)
        ohlcv_rows = run_ohlcv(universe, args.force, args.workers)
        elapsed = round(time.monotonic() - t0, 2)
        ok = sum(1 for r in ohlcv_rows if r.get("fetch_status") == "ok")
        avg = round(elapsed / len(universe), 2) if universe else 0
        timings.append({"phase": "ohlcv", "total_stocks": len(universe), "succeeded": ok,
                        "failed": len(universe) - ok, "elapsed_s": elapsed, "avg_per_stock_s": avg})

    # ── step 3: fundamentals ──
    fund_records: list[dict] = []
    if not args.skip_fundamentals:
        t0 = time.monotonic()
        print(f"\n=== Step 3: Fundamentals ===", flush=True)
        fund_records = run_fundamentals(universe, args.force)
        elapsed = round(time.monotonic() - t0, 2)
        ok = sum(1 for r in fund_records if r.get("fetch_status") == "ok")
        avg = round(elapsed / len(universe), 2) if universe else 0
        timings.append({"phase": "fundamentals", "total_stocks": len(universe), "succeeded": ok,
                        "failed": len(universe) - ok, "elapsed_s": elapsed, "avg_per_stock_s": avg})

    # ── step 4: report ──
    print(f"\n=== Step 4: Report ===", flush=True)
    build_report(universe, ohlcv_rows, fund_records, timings)

    print(f"\n=== Done ===")
    for t in timings:
        ok_str = f"{t['succeeded']}/{t['total_stocks']}"
        print(f"  {t['phase']:15s} {ok_str:12s} {t['elapsed_s']}s")


if __name__ == "__main__":
    main()
