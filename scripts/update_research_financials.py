#!/usr/bin/env python3
"""
update_research_financials.py — 季度财务快照更新脚本

每个在 config/research_stocks.json 中注册的研究股票：
1. 通过 akshare stock_financial_abstract 获取近 6 年年报财务数据
2. 通过东方财富 RPT_F10_EH_HOLDERNUM 获取最新股东人数
3. 通过 akshare stock_circulate_stock_holder 获取前十大流通股东
4. 写出 docs-site/data/{key}-financials.json
5. 发布 JSON 到 /var/www/overview/data/

建议：每季报披露后手动运行一次，或每月 1 日定时运行。
脚本任意股票失败都 exit(1)，由 cron-wrapper 触发告警邮件。
"""

import json
import math
import os
import pathlib
import shutil
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone

import akshare as ak
import requests

warnings.filterwarnings("ignore")

# ── paths ──────────────────────────────────────────────────────────────────────
REPO_DIR = pathlib.Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_DIR / "config" / "research_stocks.json"

DOCS_SITE_DIR = pathlib.Path(os.path.expanduser("~/docs-site"))
DATA_DIR = DOCS_SITE_DIR / "data"
DEPLOY_DATA_DIR = pathlib.Path("/var/www/overview/data")

BJT = timezone(timedelta(hours=8))

# 取近 N 个年报（不含预测）
MAX_ANNUAL_YEARS = 6


# ── helper ─────────────────────────────────────────────────────────────────────
def _safe(v) -> float | None:
    """将 pandas/float NaN 规整为 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _get(df, indicator: str, selection: str, col: str) -> float | None:
    rows = df[(df["指标"] == indicator) & (df["选项"] == selection)]
    if rows.empty:
        return None
    return _safe(rows.iloc[0][col])


# ── financial statements ───────────────────────────────────────────────────────
def fetch_financials(symbol: str) -> dict:
    """拉取 akshare 财务摘要，返回年度列表 + 最近季报。"""
    df = ak.stock_financial_abstract(symbol=symbol)

    # 年度列：YYYYMMDD 以 1231 结尾，降序取前 N 年
    all_date_cols = [c for c in df.columns if c.isdigit() and len(c) == 8]
    annual_cols = sorted([c for c in all_date_cols if c.endswith("1231")], reverse=True)[:MAX_ANNUAL_YEARS]

    # 最新季报列（非年报，取最新一期）
    non_annual = [c for c in sorted(all_date_cols, reverse=True) if not c.endswith("1231")]
    latest_q_col = non_annual[0] if non_annual else None

    def build_annual_entry(col: str) -> dict:
        revenue = _get(df, "营业总收入", "常用指标", col)
        profit = _get(df, "归母净利润", "常用指标", col)
        cfo = _get(df, "经营现金流量净额", "常用指标", col)
        gross_margin = _get(df, "毛利率", "常用指标", col)
        net_margin = _get(df, "销售净利率", "常用指标", col)
        roe = _get(df, "净资产收益率(ROE)", "常用指标", col)
        debt_ratio = _get(df, "资产负债率", "常用指标", col)
        revenue_yoy = _get(df, "营业总收入增长率", "成长能力", col)
        profit_yoy = _get(df, "归属母公司净利润增长率", "成长能力", col)

        entry: dict = {
            "year": col[:4] + "A",
            "revenue_yi": round(revenue / 1e8) if revenue else None,
            "revenue_yoy_pct": round(revenue_yoy, 1) if revenue_yoy is not None else None,
            "profit_yi": round(profit / 1e8) if profit else None,
            "profit_yoy_pct": round(profit_yoy, 1) if profit_yoy is not None else None,
            "gross_margin_pct": round(gross_margin, 1) if gross_margin is not None else None,
            "net_margin_pct": round(net_margin, 1) if net_margin is not None else None,
            "roe_pct": round(roe, 2) if roe is not None else None,
            "debt_ratio_pct": round(debt_ratio, 2) if debt_ratio is not None else None,
        }
        if cfo is not None:
            entry["cfo_yi"] = round(cfo / 1e8, 1)
        if cfo is not None and profit:
            entry["cfo_quality"] = round(cfo / profit, 2)
        return entry

    annual = [build_annual_entry(c) for c in reversed(annual_cols)]

    # 最新季报
    latest_q = None
    if latest_q_col:
        col = latest_q_col
        month = int(col[4:6])
        q_num = {3: "Q1", 6: "Q2", 9: "Q3"}.get(month, f"Q{month // 3}")
        label = col[:4] + q_num + "A"

        revenue = _get(df, "营业总收入", "常用指标", col)
        profit = _get(df, "归母净利润", "常用指标", col)
        gross_margin = _get(df, "毛利率", "常用指标", col)
        net_margin = _get(df, "销售净利率", "常用指标", col)
        revenue_yoy = _get(df, "营业总收入增长率", "成长能力", col)
        profit_yoy = _get(df, "归属母公司净利润增长率", "成长能力", col)

        latest_q = {
            "label": label,
            "end_date": f"{col[:4]}-{col[4:6]}-{col[6:8]}",
            "revenue_yi": round(revenue / 1e8) if revenue else None,
            "revenue_yoy_pct": round(revenue_yoy, 1) if revenue_yoy is not None else None,
            "profit_yi": round(profit / 1e8) if profit else None,
            "profit_yoy_pct": round(profit_yoy, 1) if profit_yoy is not None else None,
            "gross_margin_pct": round(gross_margin, 1) if gross_margin is not None else None,
            "net_margin_pct": round(net_margin, 1) if net_margin is not None else None,
        }

    return {"annual": annual, "latest_quarter": latest_q}


# ── shareholders ───────────────────────────────────────────────────────────────
_EM_HOLDERNUM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def fetch_shareholders(symbol: str, exchange: str) -> dict:
    """拉取股东人数 + 前十大流通股东。"""
    # 股东人数（东方财富）
    params = {
        "reportName": "RPT_F10_EH_HOLDERNUM",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{symbol}")',
        "pageSize": 1,
        "sortColumns": "END_DATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    r = requests.get(
        _EM_HOLDERNUM_URL,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    if not d.get("success") or not (d.get("result") or {}).get("data"):
        raise ValueError(f"RPT_F10_EH_HOLDERNUM returned no data for {symbol}")

    latest = d["result"]["data"][0]
    report_date = latest["END_DATE"][:10]
    total_count = int(latest["HOLDER_TOTAL_NUM"])
    count_change_pct = _safe(latest.get("TOTAL_NUM_RATIO"))
    if count_change_pct is not None:
        count_change_pct = round(count_change_pct, 1)

    # 前十大流通股东（akshare）
    df_sh = ak.stock_circulate_stock_holder(symbol=symbol)
    latest_period = df_sh["截止日期"].max()
    top10_df = df_sh[df_sh["截止日期"] == latest_period].head(10)

    top10 = [
        {
            "rank": int(row["编号"]),
            "name": row["股东名称"],
            "pct": float(row["占流通股比例"]),
            "shares": int(row["持股数量"]),
            "type": row["股本性质"],
        }
        for _, row in top10_df.iterrows()
    ]

    return {
        "report_date": report_date,
        "total_count": total_count,
        "count_change_pct": count_change_pct,
        "top10": top10,
    }


# ── main builder ───────────────────────────────────────────────────────────────
def build_financials(stock: dict) -> dict:
    symbol = stock["symbol"]
    exchange = stock["exchange"]

    print(f"  [{symbol}] 拉取财务报表...", flush=True)
    fin = fetch_financials(symbol)
    time.sleep(0.5)

    print(f"  [{symbol}] 拉取股东数据...", flush=True)
    shareholders = fetch_shareholders(symbol, exchange)

    latest_annual = fin["annual"][-1] if fin["annual"] else {}

    return {
        "symbol": symbol,
        "name": stock["name"],
        "updated_at": datetime.now(BJT).isoformat(),
        "annual": fin["annual"],
        "latest_quarter": fin["latest_quarter"],
        "latest_annual": latest_annual,
        "shareholders": shareholders,
    }


def write_and_deploy(key: str, data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEPLOY_DATA_DIR.mkdir(parents=True, exist_ok=True)

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    src = DATA_DIR / f"{key}-financials.json"
    src.write_text(json_str, encoding="utf-8")

    dst = DEPLOY_DATA_DIR / f"{key}-financials.json"
    shutil.copy2(src, dst)
    print(f"  [{key}] financials 写出: {src} → {dst}", flush=True)


# ── entry point ────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"=== update_research_financials ({datetime.now(BJT):%Y-%m-%d %H:%M} BJT) ===")

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
            data = build_financials(stock)
            write_and_deploy(stock["snapshot_key"], data)
            la = data["latest_annual"]
            q = data["latest_quarter"] or {}
            print(
                f"  [{symbol}] ✓  最新年报={la.get('year')} "
                f"营收={la.get('revenue_yi')}亿  利润={la.get('profit_yi')}亿  "
                f"ROE={la.get('roe_pct')}%  "
                f"最近季报={q.get('label')} 营收={q.get('revenue_yi')}亿"
            )
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
