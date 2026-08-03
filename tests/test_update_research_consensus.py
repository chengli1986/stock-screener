#!/usr/bin/env python3
"""test_update_research_consensus.py — 一致预期自动抓取的单元测试

背景:`config/research_stocks.json` 里的一致预期(估值分母)自各股注册日起从未更新
(git log -S 验证:旭创 2026-05-03、寒武纪 2026-05-06、茅台 2026-07-07),而市值(分子)
每日自动刷新,导致页面上的动态 PE/PS 是「今天的价 ÷ 三个月前的预期」。

本模块测试把分母也自动化的解析层。数据源=同花顺
`ak.stock_profit_forecast_ths`,fixture 为 2026-08-03 真实捕获的响应。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# ── 加载被测脚本(scripts/ 不是包,用 importlib 按路径加载)──────────────────────
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_consensus.py"
_spec = importlib.util.spec_from_file_location("update_research_consensus", _SCRIPT)
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ths_consensus"


def _fixture(name: str) -> tuple[list[str], list[list[str]]]:
    rec = json.loads((_FIXTURES / f"{name}.json").read_text())
    return rec["columns"], rec["rows"]


# ── parse_amount:同花顺金额字符串 → 元 ────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("959.68亿", 95_968_000_000.0),
        ("5629.30万", 56_293_000.0),
        ("-1.50亿", -150_000_000.0),
        ("-6826.47万", -68_264_700.0),
        ("1,666.82亿", 166_682_000_000.0),  # 千分位
        ("31.15", 31.15),  # 无单位=原值
    ],
)
def test_parse_amount_handles_units(raw, expected):
    assert urc.parse_amount(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["--", "", None, "不适用", "N/A"])
def test_parse_amount_returns_none_for_unparseable(raw):
    assert urc.parse_amount(raw) is None


# ── parse_ths_detail:详细指标预测表 → {年度: {revenue, profit}} ────────────────


def test_parse_ths_detail_extracts_forecast_years():
    """预测列 `预测2026-平均` 应解析成 2026E,营收/净利单位换算到元。"""
    parsed = urc.parse_ths_detail(*_fixture("300308_detail"))

    assert parsed["2026E"]["revenue"] == pytest.approx(95_968_000_000.0)
    assert parsed["2026E"]["profit"] == pytest.approx(30_394_000_000.0)
    assert parsed["2027E"]["profit"] == pytest.approx(53_534_000_000.0)
    assert parsed["2028E"]["revenue"] == pytest.approx(233_407_000_000.0)


def test_parse_ths_detail_extracts_actual_years():
    """实际值列 `2025-实际值` 应解析成 2025A —— 用来核对预测起点是否合理。"""
    parsed = urc.parse_ths_detail(*_fixture("300308_detail"))

    assert parsed["2025A"]["revenue"] == pytest.approx(38_240_000_000.0)
    assert parsed["2025A"]["profit"] == pytest.approx(10_797_000_000.0)


def test_parse_ths_detail_handles_wan_unit_and_negative():
    """盛科通信净利以「万」为单位且历史为负,不能被当成亿或丢成 None。"""
    parsed = urc.parse_ths_detail(*_fixture("688702_detail"))

    assert parsed["2025A"]["profit"] == pytest.approx(-150_000_000.0)  # -1.50亿
    assert parsed["2026E"]["profit"] == pytest.approx(56_000_000.0)  # 5600.00万
    assert parsed["2026E"]["revenue"] == pytest.approx(1_729_000_000.0)


def test_parse_ths_detail_ignores_non_amount_rows():
    """增长率/ROE/市盈率等行不是金额,不应混进结果。"""
    parsed = urc.parse_ths_detail(*_fixture("300308_detail"))

    for year, fields in parsed.items():
        assert set(fields) <= {"revenue", "profit"}, f"{year} 混入了非金额字段"


# ── parse_ths_dispersion:预测年报净利润表 → 机构数/最小/均值/最大 ─────────────


def test_parse_ths_dispersion_extracts_org_count_and_range():
    """均值必须带上离散度,否则无法判断它是不是被极端值拉动的。"""
    disp = urc.parse_ths_dispersion(*_fixture("300308_profit"))

    assert disp["2026"]["orgs"] == 31
    assert disp["2026"]["min"] == pytest.approx(23_302_000_000.0)
    assert disp["2026"]["mean"] == pytest.approx(30_394_000_000.0)
    assert disp["2026"]["max"] == pytest.approx(40_531_000_000.0)
    assert disp["2027"]["orgs"] == 31


def test_parse_ths_dispersion_computes_spread_ratio():
    """max/min 比值是复核时最直观的分歧度指标。"""
    disp = urc.parse_ths_dispersion(*_fixture("300308_profit"))

    assert disp["2027"]["spread_ratio"] == pytest.approx(799.82 / 339.90, rel=1e-3)


# ── compare_with_registry:与冻结值对比 ────────────────────────────────────────


def test_compare_with_registry_computes_delta_pct():
    stock = {
        "symbol": "300308",
        "name": "中际旭创",
        "consensus": {
            "2026E": {"profit_yuan": 28_500_000_000},
            "2027E": {"profit_yuan": 48_000_000_000},
        },
    }
    parsed = {"2026E": {"profit": 30_394_000_000.0}, "2027E": {"profit": 53_534_000_000.0}}

    deltas = urc.compare_with_registry(stock, parsed)

    by_key = {(d["year"], d["field"]): d for d in deltas}
    assert by_key[("2026E", "profit")]["delta_pct"] == pytest.approx(6.6, abs=0.1)
    assert by_key[("2027E", "profit")]["delta_pct"] == pytest.approx(11.5, abs=0.1)
    assert by_key[("2026E", "profit")]["frozen"] == 28_500_000_000


def test_compare_with_registry_only_compares_registered_fields():
    """注册表只登记了营收的股票(PS 模式),不应凭空生成净利对比行。"""
    stock = {
        "symbol": "688702",
        "name": "盛科通信",
        "consensus": {"2026E": {"revenue_yuan": 2_000_000_000}},
    }
    parsed = {"2026E": {"revenue": 1_729_000_000.0, "profit": 56_000_000.0}}

    deltas = urc.compare_with_registry(stock, parsed)

    assert [(d["year"], d["field"]) for d in deltas] == [("2026E", "revenue")]
    assert deltas[0]["delta_pct"] == pytest.approx(-13.55, abs=0.1)


def test_compare_with_registry_marks_missing_latest_without_crashing():
    """上游没给某年预测时应标记为缺失,而不是抛异常或算出假的 0%。"""
    stock = {
        "symbol": "000636",
        "name": "风华高科",
        "consensus": {"2028E": {"profit_yuan": 1_000_000_000}},
    }

    deltas = urc.compare_with_registry(stock, {"2026E": {"profit": 5.0}})

    assert deltas[0]["latest"] is None
    assert deltas[0]["delta_pct"] is None


# ── significant_changes:告警门槛 ──────────────────────────────────────────────


def test_significant_changes_filters_by_threshold():
    deltas = [
        {"year": "2026E", "field": "profit", "delta_pct": 6.6},
        {"year": "2027E", "field": "profit", "delta_pct": 52.3},
        {"year": "2027E", "field": "revenue", "delta_pct": -16.5},
        {"year": "2026E", "field": "revenue", "delta_pct": None},
    ]

    flagged = urc.significant_changes(deltas, threshold_pct=10.0)

    assert [d["delta_pct"] for d in flagged] == [52.3, -16.5]


def test_significant_changes_treats_threshold_as_absolute_value():
    """下修和上调同等重要 —— 只看绝对幅度。"""
    flagged = urc.significant_changes([{"delta_pct": -10.0}, {"delta_pct": 9.9}], threshold_pct=10.0)

    assert len(flagged) == 1
    assert flagged[0]["delta_pct"] == -10.0


# ── build_record:落盘 JSON 结构 ───────────────────────────────────────────────


def test_build_record_carries_provenance_and_dispersion():
    """落盘必须带来源和抓取时间,否则下一个人无法判断这份数据是不是又变成了化石。"""
    stock = {"symbol": "300308", "name": "中际旭创", "snapshot_key": "300308",
             "consensus": {"2026E": {"profit_yuan": 28_500_000_000}}}
    parsed = {"2026E": {"revenue": 95_968_000_000.0, "profit": 30_394_000_000.0}}
    disp = {"2026": {"orgs": 31, "min": 2.3302e10, "mean": 3.0394e10,
                     "max": 4.0531e10, "spread_ratio": 1.74}}

    rec = urc.build_record(stock, parsed, disp, fetched_at="2026-08-03T15:00:00+08:00")

    assert rec["symbol"] == "300308"
    assert rec["source"] == "ths"
    assert rec["fetched_at"] == "2026-08-03T15:00:00+08:00"
    assert rec["estimates"]["2026E"]["profit_yuan"] == 30_394_000_000.0
    assert rec["estimates"]["2026E"]["orgs"] == 31
    assert rec["estimates"]["2026E"]["spread_ratio"] == pytest.approx(1.74)
    assert rec["deltas_vs_registry"][0]["delta_pct"] == pytest.approx(6.6, abs=0.1)
