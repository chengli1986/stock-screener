#!/usr/bin/env python3
"""
update_research_snapshots.py — 每日快照更新脚本

每个在 config/research_stocks.json 中注册的研究股票：
1. 通过东方财富 push2 获取最新收盘价、总市值
2. 通过腾讯财经 K 线获取近 252 交易日收盘价并计算 1 年涨幅
3. 用市值除以各期共识净利润，计算动态 PE
4. 写出 docs-site/data/{key}-snapshot.json
5. 发布 JSON 文件到 /var/www/overview/data/

脚本任意股票失败都 exit(1)，由 cron-wrapper.sh 触发告警邮件。
"""

import json
import os
import pathlib
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ── HTTP retry helper ──────────────────────────────────────────────────────────


def _get_with_retry(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 15,
    max_attempts: int = 3,
    backoff: float = 1.0,
    label: str = "",
) -> requests.Response:
    """HTTP GET with retry on 5xx and network errors.

    4xx fails immediately (client-side bug shouldn't be retried). 5xx +
    ConnectionError + Timeout trigger exponential backoff. Last-attempt
    failure propagates the original exception. ``label`` shows up in retry
    warnings, useful when multiple stocks/endpoints share this code path.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt >= max_attempts:
                raise
            wait = backoff * (2 ** (attempt - 1))
            print(
                f"WARN: {label} {type(e).__name__}, retrying "
                f"({attempt + 1}/{max_attempts}) in {wait:.1f}s...",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait)
            continue
        if r.status_code >= 500 and attempt < max_attempts:
            wait = backoff * (2 ** (attempt - 1))
            print(
                f"WARN: {label} HTTP {r.status_code}, retrying "
                f"({attempt + 1}/{max_attempts}) in {wait:.1f}s...",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("_get_with_retry: unreachable")


# ── paths ──────────────────────────────────────────────────────────────────────
REPO_DIR = pathlib.Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_DIR / "config" / "research_stocks.json"

DOCS_SITE_DIR = pathlib.Path(os.path.expanduser("~/docs-site"))
DATA_DIR = DOCS_SITE_DIR / "data"
DEPLOY_DATA_DIR = pathlib.Path("/var/www/overview/data")

BJT = timezone(timedelta(hours=8))

# ── East Money push2 ───────────────────────────────────────────────────────────
_EM_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_EM_FIELDS = "f57,f58,f43,f116,f162,f163,f170,f47"
# f57=symbol, f58=name, f43=price(×100), f116=市值(元), f162=PE-TTM-s(×100), f163=PE-TTM-d(×100)
# f170=涨跌幅%(×100), f47=成交量(手)


def em_secid(symbol: str, exchange: str) -> str:
    if exchange == "SH":
        return f"1.{symbol}"
    if exchange == "SZ":
        return f"0.{symbol}"
    raise ValueError(f"Unknown exchange: {exchange}")


def fetch_em_data(symbol: str, exchange: str) -> dict:
    """获取东方财富 push2 实时行情。返回 price_yuan, market_cap_yuan。"""
    secid = em_secid(symbol, exchange)
    r = _get_with_retry(
        _EM_URL,
        params={"secid": secid, "fields": _EM_FIELDS, "ut": _EM_UT},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
        label=f"[{symbol}] push2",
    )
    data = r.json().get("data")
    if not data:
        raise ValueError(f"push2 returned empty data for {symbol}")

    raw_price = data.get("f43") or 0
    market_cap = data.get("f116") or 0
    if not raw_price or not market_cap:
        raise ValueError(f"push2 missing price or market_cap for {symbol}: f43={raw_price}, f116={market_cap}")

    price_yuan = round(raw_price / 100, 2)
    _vc = data.get("f170")
    raw_change = int(_vc) if isinstance(_vc, (int, float)) else 0
    _vv = data.get("f47")
    raw_vol = int(_vv) if isinstance(_vv, (int, float)) else 0
    change_pct = round(raw_change / 100, 2)
    vol_wan_shou = round(raw_vol / 10000, 1)
    return {
        "price_yuan": price_yuan,
        "market_cap_yuan": market_cap,
        "change_pct": change_pct,
        "vol_wan_shou": vol_wan_shou,
    }


# ── Tencent K-line ─────────────────────────────────────────────────────────────
_TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def fetch_ohlcv_data(symbol: str, exchange: str) -> dict:
    """获取近一年前复权 OHLCV，计算年涨幅 + MA20 + 波动率 + 量比。
    腾讯 K 线行格式: [date, open, close, high, low, volume, amount, ...]
    """
    import math

    mkt = "sh" if exchange == "SH" else "sz"
    code = f"{mkt}{symbol}"
    end_dt = datetime.now(BJT)
    start_dt = end_dt - timedelta(days=366)
    r = _get_with_retry(
        _TENCENT_URL,
        params={"param": f"{code},day,{start_dt:%Y-%m-%d},{end_dt:%Y-%m-%d},300,qfq"},
        headers={"Referer": "https://gu.qq.com/"},
        timeout=15,
        label=f"[{symbol}] tencent-kline",
    )
    d = r.json()
    stock_data = d.get("data", {}).get(code, {})
    rows = stock_data.get("qfqday") or stock_data.get("day", [])
    if len(rows) < 60:
        raise ValueError(f"Too few K-line rows for {symbol}: {len(rows)}")

    closes  = [float(row[2]) for row in rows if len(row) >= 3]
    highs   = [float(row[3]) for row in rows if len(row) >= 5]
    lows    = [float(row[4]) for row in rows if len(row) >= 5]
    volumes = [float(row[5]) for row in rows if len(row) >= 6]

    # 1 年涨幅
    window = closes[-252:] if len(closes) >= 252 else closes
    year_return_pct = round((window[-1] / window[0] - 1) * 100, 1)

    # MA20（当日 + 5 日前，判断斜率）
    ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else None
    ma20_5d = round(sum(closes[-25:-5]) / 20, 2) if len(closes) >= 25 else None
    ma20_slope = "up" if (ma20 and ma20_5d and ma20 > ma20_5d) else "down"

    # 60 日年化波动率（对数收益率标准差 × √252）
    vol_60d_ann_pct: float | None = None
    if len(closes) >= 61:
        lr = [math.log(closes[-60 + i + 1] / closes[-60 + i]) for i in range(59)]
        daily_std = math.sqrt(sum(x * x for x in lr) / len(lr))
        vol_60d_ann_pct = round(daily_std * math.sqrt(252) * 100, 1)

    # 5 日 / 60 日量比
    vol_ratio_5_60: float | None = None
    if len(volumes) >= 60:
        avg5 = sum(volumes[-5:]) / 5
        avg60 = sum(volumes[-60:]) / 60
        vol_ratio_5_60 = round(avg5 / avg60, 2) if avg60 > 0 else None

    # 52 周（最近252交易日）高低
    win_h = highs[-252:] if len(highs) >= 252 else highs
    win_l = lows[-252:]  if len(lows)  >= 252 else lows
    week52_high = round(max(win_h), 2) if win_h else None
    week52_low  = round(min(win_l), 2) if win_l else None

    return {
        "year_return_pct": year_return_pct,
        "ma20": ma20,
        "ma20_slope": ma20_slope,
        "vol_60d_ann_pct": vol_60d_ann_pct,
        "vol_ratio_5_60": vol_ratio_5_60,
        "week52_high": week52_high,
        "week52_low": week52_low,
    }


# ── snapshot writer ─────────────────────────────────────────────────────────────

def build_snapshot(stock: dict) -> dict:
    symbol = stock["symbol"]
    exchange = stock["exchange"]
    consensus = stock["consensus"]

    print(f"  [{symbol}] 拉取 push2 行情...", flush=True)
    em = fetch_em_data(symbol, exchange)
    price_yuan = em["price_yuan"]
    market_cap_yuan = em["market_cap_yuan"]
    change_pct = em["change_pct"]
    vol_wan_shou = em["vol_wan_shou"]

    print(f"  [{symbol}] 拉取腾讯 K 线...", flush=True)
    ohlcv = fetch_ohlcv_data(symbol, exchange)

    market_cap_yi = round(market_cap_yuan / 1e8)  # 转换为亿
    as_of = datetime.now(BJT).strftime("%Y-%m-%d")

    valuation_mode = stock.get("valuation_mode", "pe")
    pe_estimates: dict[str, float] = {}
    ps_estimates: dict[str, float] = {}
    for label, entry in consensus.items():
        if valuation_mode in ("ps", "both") and "revenue_yuan" in entry:
            ps_estimates[label] = round(market_cap_yuan / entry["revenue_yuan"], 1)
        if valuation_mode in ("pe", "both") and "profit_yuan" in entry:
            pe_estimates[label] = round(market_cap_yuan / entry["profit_yuan"], 1)

    snapshot = {
        "symbol": symbol,
        "name": stock["name"],
        "as_of": as_of,
        "price_yuan": price_yuan,
        "change_pct": change_pct,
        "vol_wan_shou": vol_wan_shou,
        "market_cap_yi": market_cap_yi,
        "year_return_pct": ohlcv["year_return_pct"],
        "pe_estimates": pe_estimates,
        "ps_estimates": ps_estimates,
        "technical": {
            "ma20": ohlcv["ma20"],
            "ma20_slope": ohlcv["ma20_slope"],
            "vol_60d_ann_pct": ohlcv["vol_60d_ann_pct"],
            "vol_ratio_5_60": ohlcv["vol_ratio_5_60"],
            "week52_high": ohlcv["week52_high"],
            "week52_low": ohlcv["week52_low"],
        },
        "updated_at": datetime.now(BJT).isoformat(),
    }
    return snapshot


def write_and_deploy(key: str, snapshot: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEPLOY_DATA_DIR.mkdir(parents=True, exist_ok=True)

    json_str = json.dumps(snapshot, ensure_ascii=False, indent=2)
    src = DATA_DIR / f"{key}-snapshot.json"
    src.write_text(json_str, encoding="utf-8")

    dst = DEPLOY_DATA_DIR / f"{key}-snapshot.json"
    shutil.copy2(src, dst)
    print(f"  [{key}] snapshot 写出: {src} → {dst}", flush=True)


# ── main ────────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"=== update_research_snapshots ({datetime.now(BJT):%Y-%m-%d %H:%M} BJT) ===")

    if not CONFIG_FILE.exists():
        print(f"ERROR: config not found: {CONFIG_FILE}", file=sys.stderr)
        return 1

    stocks = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not stocks:
        print("WARNING: research_stocks.json is empty — nothing to do")
        return 0

    errors: list[str] = []

    for stock in stocks:
        symbol = stock["symbol"]
        try:
            snapshot = build_snapshot(stock)
            write_and_deploy(stock["snapshot_key"], snapshot)
            market_cap_yi = snapshot["market_cap_yi"]
            price = snapshot["price_yuan"]
            yr = snapshot["year_return_pct"]
            if snapshot["ps_estimates"]:
                val_str = "  ".join(f"{k}={v}x PS" for k, v in snapshot["ps_estimates"].items())
            else:
                val_str = "  ".join(f"{k}={v}x" for k, v in snapshot["pe_estimates"].items())
            print(f"  [{symbol}] ✓  ¥{price}  市值{market_cap_yi}亿  1年{yr:+.1f}%  {val_str}")
        except Exception as e:
            msg = f"[{symbol}] FAILED: {e}"
            print(f"ERROR: {msg}", file=sys.stderr)
            errors.append(msg)
        time.sleep(0.5)

    if errors:
        print(f"\n=== FAILED ({len(errors)}/{len(stocks)}) ===", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print(f"\n=== done ({len(stocks)} stocks updated) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
