#!/usr/bin/env python3
"""test_financials_hk_yfinance.py — 港股财务报表走 yfinance

触发（2026-08-04 `research-data-health` 告警）：`02513-financials.json` 41 天未更新。
**根因不是 cron 故障** —— `update_research_financials.py` 按设计跳过港股
（akshare `stock_financial_abstract` 只支持 A 股），文件自 2026-06-24 冻结。

**但真问题比告警说的严重**：`latest_annual` 停在 **2024A（营收 3.124 亿）**，
而 2025 年报早已发布（营收 7.24 亿 / 净利 -47.18 亿）——研报页的财务表少了一整个财年。

yfinance 已验证可给 2022–2025 四年完整数据，七个字段全覆盖。

## 两个必须处理的陷阱

1. **股东权益为负**：智谱 2025 年 Stockholders Equity = **-80.93 亿**
   （优先股/可转换工具在上市前计入负债，权益为负是港股新经济公司常见形态）。
   ROE = 净利 / 负权益 会得到一个**正数**，看起来像盈利——必须留空而不是算。
2. **同比需要上一年**：最早那一年没有上一年，`revenue_yoy_pct` 必须是 None 而非 0。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_financials.py"
_spec = importlib.util.spec_from_file_location("update_research_financials", _SCRIPT)
urf = importlib.util.module_from_spec(_spec)
sys.modules["update_research_financials"] = urf
_spec.loader.exec_module(urf)


# 智谱 2513.HK 实测值（单位：元）；列按 yfinance 惯例新→旧
_INCOME = {
    "Total Revenue":                  {"2025": 7.24e8,  "2024": 3.124e8},
    "Gross Profit":                   {"2025": 2.97e8,  "2024": 1.76e8},
    "Net Income Common Stockholders": {"2025": -47.18e8, "2024": -29.58e8},
}
_BALANCE = {
    "Stockholders Equity":                    {"2025": -80.93e8, "2024": -39.57e8},
    "Total Assets":                           {"2025": 48.54e8,  "2024": 43.76e8},
    "Total Liabilities Net Minority Interest": {"2025": 129.65e8, "2024": 83.31e8},
}
_CASHFLOW = {"Operating Cash Flow": {"2025": -22.46e8, "2024": -22.45e8}}


def _annual():
    return urf.build_hk_annual(_INCOME, _BALANCE, _CASHFLOW, years=["2024", "2025"])


# ── 基本换算与结构 ────────────────────────────────────────────────────────────


def test_annual_is_ascending_by_year():
    """与 A 股一致：旧→新，`annual[-1]` 即最新年报。"""
    rows = _annual()

    assert [r["year"] for r in rows] == ["2024A", "2025A"]


def test_converts_to_yi_with_two_decimals():
    latest = _annual()[-1]

    assert latest["revenue_yi"] == pytest.approx(7.24)
    assert latest["profit_yi"] == pytest.approx(-47.18)


def test_schema_matches_a_share_fields():
    """字段名必须与 A 股同构，页面水合逻辑才不用分市场处理。"""
    latest = _annual()[-1]

    for k in ("year", "revenue_yi", "revenue_yoy_pct", "profit_yi",
              "gross_margin_pct", "net_margin_pct", "debt_ratio_pct", "cfo_yi"):
        assert k in latest, f"缺字段 {k}"


def test_yoy_computed_from_previous_year():
    latest = _annual()[-1]

    assert latest["revenue_yoy_pct"] == pytest.approx(131.8, abs=0.5)  # 7.24/3.124-1


def test_earliest_year_has_no_yoy():
    """最早一年没有上一年 —— 同比必须是 None，不能当作 0%。"""
    assert _annual()[0]["revenue_yoy_pct"] is None


def test_margins_computed():
    latest = _annual()[-1]

    assert latest["gross_margin_pct"] == pytest.approx(41.0, abs=0.5)   # 2.97/7.24
    assert latest["net_margin_pct"] == pytest.approx(-651.7, abs=2)     # -47.18/7.24


def test_debt_ratio_from_liabilities_over_assets():
    latest = _annual()[-1]

    assert latest["debt_ratio_pct"] == pytest.approx(267.1, abs=1)      # 129.65/48.54


# ── ★负权益：ROE 必须留空 ────────────────────────────────────────────────────


def test_roe_is_none_when_equity_negative():
    """智谱 2025 权益 -80.93 亿。ROE = -47.18 / -80.93 = +58.3%，
    **看起来像高盈利**——这是负负得正的假象，必须留空。"""
    latest = _annual()[-1]

    assert latest["roe_pct"] is None, "负权益下算出了 ROE"


def test_roe_computed_when_equity_positive():
    bs = {**_BALANCE, "Stockholders Equity": {"2025": 100e8, "2024": 80e8}}

    rows = urf.build_hk_annual(_INCOME, bs, _CASHFLOW, years=["2024", "2025"])

    assert rows[-1]["roe_pct"] == pytest.approx(-47.2, abs=0.5)


def test_negative_equity_flagged_for_the_reader():
    """留空还不够——要让页面能说明「为什么没有 ROE」。"""
    latest = _annual()[-1]

    assert latest.get("equity_negative") is True


# ── 缺失容错 ──────────────────────────────────────────────────────────────────


def test_missing_metric_yields_none_not_zero():
    rows = urf.build_hk_annual({"Total Revenue": {"2025": 7.24e8}}, {}, {}, years=["2025"])

    assert rows[-1]["revenue_yi"] == pytest.approx(7.24)
    assert rows[-1]["profit_yi"] is None
    assert rows[-1]["cfo_yi"] is None


def test_empty_input_yields_empty_list():
    assert urf.build_hk_annual({}, {}, {}, years=[]) == []


def test_zero_revenue_does_not_divide_by_zero():
    rows = urf.build_hk_annual(
        {"Total Revenue": {"2025": 0}, "Gross Profit": {"2025": 1e8}}, {}, {}, years=["2025"])

    assert rows[-1]["gross_margin_pct"] is None
