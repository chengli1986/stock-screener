#!/usr/bin/env python3
"""test_consensus_hk_yfinance.py — 港股一致预期走 yfinance

分工（2026-08-04 用户确认）：**A 股来源不变**（同花顺主 + 东财 F10 交叉），
**港股全部走 yfinance**。理由是覆盖天然对齐——同花顺不覆盖港股、东财 F10 的
ProfitForecast 仅支持 A 股、Longbridge 账户无基本面数据权限（实测连腾讯
0700.HK 的 financial-report 都返回空）。

两个必须处理的细节：

1. **代码格式**：yfinance 港股用 4 位零填充。实测 `2513.HK` ✓ / `02513.HK` ✗；
   `0700.HK` ✓ / `700.HK` ✗。注册表里智谱存的是 `02513`，需转换。
2. **相对期 → 绝对年份**：yfinance 返回 `0y` / `+1y` 是「当前财年 / 下一财年」，
   不是固定年份，跨年会自动平移。锚点用 `nextFiscalYearEnd`
   （智谱实测 2026-12-31 → `0y` = 2026E），按实际财年落盘而非写死。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_consensus.py"
_spec = importlib.util.spec_from_file_location("update_research_consensus", _SCRIPT)
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)

_FIX = Path(__file__).resolve().parent / "fixtures" / "yf_consensus"
_PAYLOAD = json.loads((_FIX / "2513HK.json").read_text())


# ── 代码格式 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("symbol,expected", [
    ("02513", "2513.HK"),   # 注册表 5 位 → yfinance 4 位
    ("0700", "0700.HK"),    # 已是 4 位，保持零填充
    ("9988", "9988.HK"),
    ("00700", "0700.HK"),   # 多余前导零也要归一
])
def test_yf_ticker_pads_to_four_digits(symbol, expected):
    assert urc.yf_ticker(symbol) == expected


def test_yf_ticker_rejects_non_numeric():
    with pytest.raises(ValueError):
        urc.yf_ticker("ABC")


# ── 相对期 → 绝对年份 ─────────────────────────────────────────────────────────


def test_resolve_forecast_years_from_next_fiscal_year_end():
    """智谱实测 nextFiscalYearEnd = 2026-12-31 → 0y=2026E, +1y=2027E。"""
    ts = _PAYLOAD["info_subset"]["nextFiscalYearEnd"]

    years = urc.resolve_forecast_years(ts)

    assert years["0y"] == "2026E"
    assert years["+1y"] == "2027E"


def test_resolve_forecast_years_handles_non_december_fiscal_end():
    """3 月财年末（如部分港股）：2027-03-31 结束的财年记作 2027E。"""
    import datetime
    ts = int(datetime.datetime(2027, 3, 31, tzinfo=datetime.timezone.utc).timestamp())

    assert urc.resolve_forecast_years(ts)["0y"] == "2027E"


def test_resolve_forecast_years_returns_empty_without_anchor():
    """拿不到财年锚点时返回空 —— 宁可没有预测，也不能把年份猜错后写进估值分母。"""
    assert urc.resolve_forecast_years(None) == {}


# ── 解析为与 A 股同构的 estimates ─────────────────────────────────────────────


def test_parse_yf_estimates_matches_a_share_schema():
    """字段名必须与 parse_em_estimates 一致，页面水合逻辑才不用改。"""
    est = urc.parse_yf_estimates(_PAYLOAD["revenue_estimate"], _PAYLOAD["earnings_estimate"],
                                 _PAYLOAD["info_subset"]["nextFiscalYearEnd"])

    assert est["2026E"]["revenue_yuan"] == 3_730_110_510
    assert est["2026E"]["revenue_orgs"] == 16
    assert est["2027E"]["revenue_yuan"] == 9_538_914_610
    assert est["2027E"]["revenue_orgs"] == 13


def test_parse_yf_estimates_keeps_revenue_range():
    est = urc.parse_yf_estimates(_PAYLOAD["revenue_estimate"], _PAYLOAD["earnings_estimate"],
                                 _PAYLOAD["info_subset"]["nextFiscalYearEnd"])

    assert est["2026E"]["revenue_min_yuan"] == 2_484_150_000
    assert est["2026E"]["revenue_max_yuan"] == 5_498_000_000


def test_parse_yf_estimates_skips_quarterly_periods():
    """`0q` / `+1q` 是季度，不该混进年度估值分母。"""
    est = urc.parse_yf_estimates(_PAYLOAD["revenue_estimate"], _PAYLOAD["earnings_estimate"],
                                 _PAYLOAD["info_subset"]["nextFiscalYearEnd"])

    assert set(est) == {"2026E", "2027E"}


def test_parse_yf_estimates_omits_zero_revenue():
    """yfinance 对无覆盖期返回 0，不能当成「预测营收为零」。"""
    rev = {"0y": {"avg": 0, "numberOfAnalysts": 0}}

    est = urc.parse_yf_estimates(rev, {}, _PAYLOAD["info_subset"]["nextFiscalYearEnd"])

    assert "revenue_yuan" not in est.get("2026E", {})


def test_parse_yf_estimates_carries_loss_making_eps():
    """智谱在亏损：EPS 为负是合法值，不能被当作缺失丢掉。"""
    est = urc.parse_yf_estimates(_PAYLOAD["revenue_estimate"], _PAYLOAD["earnings_estimate"],
                                 _PAYLOAD["info_subset"]["nextFiscalYearEnd"])

    assert est["2026E"]["eps"] < 0
    assert est["2026E"]["eps_orgs"] == 13


# ── 评级与目标价（港股独有维度）───────────────────────────────────────────────


def test_parse_yf_ratings_maps_to_shared_schema():
    """与东财 parse_em_ratings 输出同构，下游不必分市场处理。"""
    r = urc.parse_yf_ratings(_PAYLOAD["recommendations"], _PAYLOAD["analyst_price_targets"])

    assert r["buy"] == 10
    assert r["orgs"] == 18          # 4+10+3+1
    # 断言对齐 fixture 本身，而不是某次抓取时记下的数字——目标价随行情/汇率浮动
    pt = _PAYLOAD["analyst_price_targets"]
    assert r["target_mean"] == pytest.approx(pt["mean"])
    assert r["target_median"] == pytest.approx(pt["median"])
    assert r["target_mean"] > r["target_low"]


def test_parse_yf_ratings_tolerates_missing_targets():
    r = urc.parse_yf_ratings(_PAYLOAD["recommendations"], None)

    assert r["orgs"] == 18
    assert r["target_mean"] is None
