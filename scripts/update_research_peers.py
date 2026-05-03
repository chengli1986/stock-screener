#!/usr/bin/env python3
"""
update_research_peers.py — 全球竞争格局同行数据更新

数据源：
  - A 股同行（300502 新易盛 / 300394 天孚通信）：akshare stock_financial_abstract
  - 海外上市公司（COHR / AAOI / LITE）：yfinance 年报财务
  - 旭创自身（300308）：读取已生成的 300308-financials.json
  - USD/CNY 汇率：yfinance USDCNY=X

输出：docs-site/data/300308-peers.json → /var/www/overview/data/300308-peers.json

建议每月 1 日与 update_research_financials.py 一同运行。
"""

import json
import math
import os
import pathlib
import shutil
import sys
import warnings
from datetime import datetime, timedelta, timezone

import akshare as ak
import yfinance as yf

warnings.filterwarnings("ignore")

# ── paths ──────────────────────────────────────────────────────────────────────
REPO_DIR = pathlib.Path(__file__).resolve().parents[1]
DOCS_SITE_DIR = pathlib.Path(os.path.expanduser("~/docs-site"))
DATA_DIR = DOCS_SITE_DIR / "data"
DEPLOY_DATA_DIR = pathlib.Path("/var/www/overview/data")

BJT = timezone(timedelta(hours=8))


# ── helpers ─────────────────────────────────────────────────────────────────────
def _safe(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _round1(v) -> float | None:
    return round(v, 1) if v is not None else None


# ── exchange rate ──────────────────────────────────────────────────────────────
def fetch_usd_cny() -> float:
    """Fetch live USD/CNY spot rate via yfinance."""
    t = yf.Ticker("USDCNY=X")
    rate = _safe(t.fast_info.last_price)
    if rate is None or rate < 5 or rate > 12:
        raise ValueError(f"Unexpected USDCNY rate: {rate}")
    return round(rate, 4)


# ── overseas peers via yfinance ────────────────────────────────────────────────
_OVERSEAS_CONFIGS = [
    {
        "key": "cohr",
        "ticker": "COHR",
        "name": "Coherent Corp",
        "country": "US",
        "lc_rank": "#2",
        "position": "长距 + 短距全覆盖，InP 激光器垂直整合",
        "note": "含半导体 / 工业部门合计",
    },
    {
        "key": "aaoi",
        "ticker": "AAOI",
        "name": "Applied Optoelectronics (AAOI)",
        "country": "US",
        "lc_rank": "~#7",
        "position": "超大规模数据中心专注，低成本竞争策略",
        "note": "",
    },
    {
        "key": "lite",
        "ticker": "LITE",
        "name": "Lumentum (LITE)",
        "country": "US",
        "lc_rank": "上游",
        "position": "EML 激光器核心供应商（50–60% 全球份额）",
        "note": "",
    },
]


def fetch_overseas_peer(cfg: dict) -> dict:
    """Fetch annual financials for an overseas listed company via yfinance."""
    ticker = cfg["ticker"]
    t = yf.Ticker(ticker)
    fin = t.financials  # annual income statement, columns = fiscal year end dates descending

    if fin is None or fin.empty:
        raise ValueError(f"[{ticker}] yfinance returned empty financials")

    col = fin.columns[0]  # most recent fiscal year
    fiscal_label = f"FY{col.year}"

    rev = _safe(fin.loc["Total Revenue", col]) if "Total Revenue" in fin.index else None
    net = _safe(fin.loc["Net Income", col]) if "Net Income" in fin.index else None
    gp = _safe(fin.loc["Gross Profit", col]) if "Gross Profit" in fin.index else None

    if rev is None:
        raise ValueError(f"[{ticker}] Total Revenue not found for {col}")

    net_margin = _round1(net / rev * 100) if net is not None and rev else None
    gross_margin = _round1(gp / rev * 100) if gp is not None and rev else None
    revenue_usd_b = round(rev / 1e9, 2)

    return {
        "key": cfg["key"],
        "name": cfg["name"],
        "ticker": ticker,
        "country": "US",
        "fiscal_year": fiscal_label,
        "revenue_usd_b": revenue_usd_b,
        "net_margin_pct": net_margin,
        "gross_margin_pct": gross_margin,
        "lc_rank": cfg["lc_rank"],
        "position": cfg["position"],
        "note": cfg["note"],
    }


# ── domestic peers via akshare ─────────────────────────────────────────────────
_DOMESTIC_CONFIGS = [
    {
        "key": "eoptolink",
        "symbol": "300502",
        "name": "新易盛 Eoptolink",
        "country": "CN",
        "lc_rank": "#3（由 #7 升）",
        "position": "数据中心专注，硅光先行，国内毛利率最高",
        "note": "",
    },
    {
        "key": "tianfu",
        "symbol": "300394",
        "name": "天孚通信",
        "country": "CN",
        "lc_rank": "器件",
        "position": "无源光器件龙头（FA/MT 连接器全球 #1）",
        "note": "",
    },
]


def fetch_domestic_peer(cfg: dict, usd_cny: float) -> dict:
    """Fetch most recent annual financials for an A-share company via akshare."""
    symbol = cfg["symbol"]
    df = ak.stock_financial_abstract(symbol=symbol)

    annual_cols = sorted(
        [c for c in df.columns if c.isdigit() and len(c) == 8 and c.endswith("1231")],
        reverse=True,
    )
    if not annual_cols:
        raise ValueError(f"[{symbol}] No annual columns found")

    col = annual_cols[0]
    fiscal_label = col[:4] + "A"

    def get(indicator: str, selection: str) -> float | None:
        rows = df[(df["指标"] == indicator) & (df["选项"] == selection)]
        return _safe(rows.iloc[0][col]) if not rows.empty else None

    rev = get("营业总收入", "常用指标")
    net_margin = _round1(get("销售净利率", "常用指标"))
    gross_margin = _round1(get("毛利率", "常用指标"))

    if rev is None:
        raise ValueError(f"[{symbol}] Revenue not found for {col}")

    revenue_cny_yi = round(rev / 1e8)
    revenue_usd_b = round(rev / (usd_cny * 1e9), 2)  # raw CNY → USD billion

    return {
        "key": cfg["key"],
        "name": cfg["name"],
        "ticker": f"{symbol}.SZ",
        "country": "CN",
        "fiscal_year": fiscal_label,
        "revenue_cny_yi": revenue_cny_yi,
        "revenue_usd_b": revenue_usd_b,
        "net_margin_pct": net_margin,
        "gross_margin_pct": gross_margin,
        "lc_rank": cfg["lc_rank"],
        "position": cfg["position"],
        "note": cfg["note"],
    }


# ── 旭创自身 from financials.json ──────────────────────────────────────────────
def load_innolight_peer(usd_cny: float) -> dict:
    """Read 旭创 data from existing 300308-financials.json."""
    src = DATA_DIR / "300308-financials.json"
    if not src.exists():
        raise FileNotFoundError(f"300308-financials.json not found at {src}")

    d = json.loads(src.read_text(encoding="utf-8"))
    la = d.get("latest_annual", {})

    rev = la.get("revenue_yi")  # 亿元 CNY
    net_margin = la.get("net_margin_pct")
    gross_margin = la.get("gross_margin_pct")
    fiscal_label = la.get("year", "")

    if rev is None:
        raise ValueError("旭创 revenue_yi not found in 300308-financials.json")

    revenue_usd_b = round(rev / usd_cny * 100 / 1e9, 2)  # 亿CNY → USD B
    # correction: 亿 = 1e8, so rev (亿) * 1e8 / usd_cny / 1e9 = rev * 1e8 / (usd_cny * 1e9)
    revenue_usd_b = round(rev * 1e8 / (usd_cny * 1e9), 2)

    return {
        "key": "innolight",
        "name": "旭创 InnoLight",
        "ticker": "300308.SZ",
        "country": "CN",
        "fiscal_year": fiscal_label,
        "revenue_cny_yi": int(rev),
        "revenue_usd_b": revenue_usd_b,
        "net_margin_pct": net_margin,
        "gross_margin_pct": gross_margin,
        "lc_rank": "#1",
        "position": "数据中心绝对领先，800G 主力，1.6T 先发",
        "note": "",
    }


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"=== update_research_peers ({datetime.now(BJT):%Y-%m-%d %H:%M} BJT) ===")

    print("  拉取 USD/CNY 汇率...", flush=True)
    usd_cny = fetch_usd_cny()
    print(f"  USDCNY = {usd_cny}", flush=True)

    peers: list[dict] = []
    errors: list[str] = []

    # 旭创自身
    try:
        p = load_innolight_peer(usd_cny)
        peers.append(p)
        print(f"  [300308] ¥{p['revenue_cny_yi']}亿 / ${p['revenue_usd_b']}B  "
              f"净利率={p['net_margin_pct']}%  毛利率={p['gross_margin_pct']}%")
    except Exception as e:
        errors.append(f"[innolight] {e}")
        print(f"ERROR [300308]: {e}", file=sys.stderr)

    # 海外上市公司
    for cfg in _OVERSEAS_CONFIGS:
        try:
            p = fetch_overseas_peer(cfg)
            peers.append(p)
            print(f"  [{cfg['ticker']}] ${p['revenue_usd_b']}B  "
                  f"净利率={p['net_margin_pct']}%  毛利率={p['gross_margin_pct']}%  ({p['fiscal_year']})")
        except Exception as e:
            errors.append(f"[{cfg['ticker']}] {e}")
            print(f"ERROR [{cfg['ticker']}]: {e}", file=sys.stderr)

    # 国内同行
    for cfg in _DOMESTIC_CONFIGS:
        try:
            p = fetch_domestic_peer(cfg, usd_cny)
            peers.append(p)
            print(f"  [{cfg['symbol']}] ¥{p['revenue_cny_yi']}亿 / ${p['revenue_usd_b']}B  "
                  f"净利率={p['net_margin_pct']}%  毛利率={p['gross_margin_pct']}%  ({p['fiscal_year']})")
        except Exception as e:
            errors.append(f"[{cfg['symbol']}] {e}")
            print(f"ERROR [{cfg['symbol']}]: {e}", file=sys.stderr)

    if errors:
        print(f"\n=== FAILED ({len(errors)}) ===", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    output = {
        "updated_at": datetime.now(BJT).isoformat(),
        "usd_cny": usd_cny,
        "peers": peers,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEPLOY_DATA_DIR.mkdir(parents=True, exist_ok=True)
    src = DATA_DIR / "300308-peers.json"
    dst = DEPLOY_DATA_DIR / "300308-peers.json"
    src.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(src, dst)
    print(f"\n  写出: {src} → {dst}")
    print(f"=== done ({len(peers)} peers, USDCNY={usd_cny}) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
