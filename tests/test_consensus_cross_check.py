#!/usr/bin/env python3
"""test_consensus_cross_check.py — 一致预期的第三方自动交叉复核

背景:`update_research_consensus.py` 原本只有同花顺**单一数据源**,无从判断它对不对。
人工用 Wind 复核不可持续(用户 2026-08-03 明确指出耗时)。

2026-08-03 找到独立源:东方财富 F10
`emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax`。
它与同花顺采集口径独立,且比同花顺多给三样东西:
  1. `yctj_list` 一致预期汇总,**归母净利/营收各自带机构数**
  2. `ycmx` 逐家机构明细 + **发布日期**(可识别陈旧预测、自算中位数)
  3. `pjtj` 评级统计(买入/增持/中性/减持/卖出家数 + 综合评级)

首次全池实测:40 项比对 39 项差异 <10%,唯一超阈值的是寒武纪 2027E 营收(-10.3%)。
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

_FIX = Path(__file__).resolve().parent / "fixtures" / "em_consensus"


def _payload(code: str) -> dict:
    return json.loads((_FIX / f"{code}.json").read_text())


# ── em_secid:东财 F10 代码格式 ────────────────────────────────────────────────


@pytest.mark.parametrize("symbol,exchange,expected", [
    ("300308", "SZ", "SZ300308"),
    ("688825", "SH", "SH688825"),
    ("600519", "SH", "SH600519"),
])
def test_em_secid_prefixes_market(symbol, exchange, expected):
    assert urc.em_secid(symbol, exchange) == expected


def test_em_secid_rejects_hk():
    """港股不走这个接口 —— 静默返回错误格式会导致抓到空数据却不报错。"""
    with pytest.raises(ValueError):
        urc.em_secid("02513", "HK")


# ── parse_em_estimates:一致预期汇总 ───────────────────────────────────────────


def test_parse_em_estimates_keeps_only_forecast_years():
    """YEAR_MARK='A' 是实际值,不能混进预测。"""
    est = urc.parse_em_estimates(_payload("300308")["yctj_list"])

    assert set(est) == {"2026E", "2027E", "2028E"}


def test_parse_em_estimates_extracts_profit_revenue_and_org_counts():
    est = urc.parse_em_estimates(_payload("300308")["yctj_list"])

    assert est["2026E"]["profit_yuan"] == pytest.approx(30_800_000_000, rel=0.01)
    assert est["2026E"]["revenue_yuan"] == pytest.approx(95_100_000_000, rel=0.01)
    assert est["2026E"]["profit_orgs"] == 29
    assert est["2027E"]["profit_yuan"] == pytest.approx(54_780_000_000, rel=0.01)


def test_parse_em_estimates_handles_thin_coverage():
    """长鑫上市仅数日,覆盖机构极少,不能因字段稀疏而崩。"""
    est = urc.parse_em_estimates(_payload("688825")["yctj_list"])

    assert "2026E" in est
    assert est["2026E"]["profit_orgs"] is not None


# ── parse_em_ratings:评级统计 ─────────────────────────────────────────────────


def test_parse_em_ratings_picks_six_month_window():
    """6 月内窗口与同花顺的机构数口径最接近,用它做对照。"""
    r = urc.parse_em_ratings(_payload("300308")["pjtj"])

    assert r["window"] == "6月内"
    assert r["orgs"] == 29
    assert r["buy"] == 25
    assert r["rating"] == "买入"


def test_parse_em_ratings_returns_none_when_window_absent():
    assert urc.parse_em_ratings([{"DATE_TYPE": "1月内", "RATING_ORG_NUM": 2}]) is None


# ── cross_check:两源比对 ──────────────────────────────────────────────────────


def test_cross_check_marks_confirmed_when_sources_agree():
    ths = {"2026E": {"profit_yuan": 30_394_000_000}}
    em = {"2026E": {"profit_yuan": 30_800_000_000}}

    out = urc.cross_check(ths, em, threshold_pct=10.0)

    assert out["2026E"]["profit"]["verdict"] == "CONFIRMED"
    assert out["2026E"]["profit"]["diff_pct"] == pytest.approx(1.34, abs=0.05)


def test_cross_check_marks_divergent_beyond_threshold():
    """寒武纪 2027E 营收实测 -10.3%,必须被标出来而不是悄悄通过。"""
    ths = {"2027E": {"revenue_yuan": 31_350_000_000}}
    em = {"2027E": {"revenue_yuan": 28_110_000_000}}

    out = urc.cross_check(ths, em, threshold_pct=10.0)

    assert out["2027E"]["revenue"]["verdict"] == "DIVERGENT"
    assert out["2027E"]["revenue"]["diff_pct"] < -10


def test_cross_check_marks_unverified_when_second_source_missing():
    """港股无东财 F10 数据 —— 是「未验证」，不是「已确认」。这个区分是本模块的重点。"""
    ths = {"2026E": {"revenue_yuan": 1_000_000_000}}

    out = urc.cross_check(ths, {}, threshold_pct=10.0)

    assert out["2026E"]["revenue"]["verdict"] == "UNVERIFIED"
    assert out["2026E"]["revenue"]["diff_pct"] is None


def test_cross_check_ignores_fields_absent_from_primary():
    """同花顺没给净利时不该凭空生成一条比对（PS 模式标的常见）。"""
    ths = {"2026E": {"revenue_yuan": 1_000_000_000}}
    em = {"2026E": {"revenue_yuan": 1_010_000_000, "profit_yuan": 5_000_000}}

    out = urc.cross_check(ths, em, threshold_pct=10.0)

    assert set(out["2026E"]) == {"revenue"}


def test_cross_check_summary_counts_verdicts():
    ths = {"2026E": {"profit_yuan": 100.0, "revenue_yuan": 100.0},
           "2027E": {"profit_yuan": 100.0}}
    em = {"2026E": {"profit_yuan": 101.0, "revenue_yuan": 200.0}}

    summary = urc.cross_check_summary(urc.cross_check(ths, em, threshold_pct=10.0))

    assert summary == {"CONFIRMED": 1, "DIVERGENT": 1, "UNVERIFIED": 1}
