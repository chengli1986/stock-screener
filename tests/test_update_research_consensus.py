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


# ── 注册表冻结值对比：2026-08-09 整段删除 ─────────────────────────────────────
#
# 原有 `compare_with_registry` / `significant_changes` 用来把最新一致预期跟
# `research_stocks.json` 里的人工冻结值比，并在偏离超阈值时提示「需人工同步注册表」。
# 用户当天拍板**删掉注册表兜底**（宁可不显示，也不显示一个用登记日旧预期算出的错 PE），
# 冻结值随之从配置里清空——这两个函数的输入永远为空，产出永远是空列表，
# 留着只会让人以为还有一道人工校验。故连同 `deltas_vs_registry` 字段一并删除。
# 新契约见 tests/test_snapshot_consensus_source.py。


# ── build_record:落盘 JSON 结构 ───────────────────────────────────────────────


def test_build_record_carries_provenance_and_dispersion():
    """落盘必须带来源和抓取时间,否则下一个人无法判断这份数据是不是又变成了化石。"""
    stock = {"symbol": "300308", "name": "中际旭创", "snapshot_key": "300308"}
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
    assert "deltas_vs_registry" not in rec, "注册表对比已于 2026-08-09 删除"


def test_build_record_rounds_away_float_noise():
    """`亿 → 元` 的 ×1e8 会留下浮点尾巴(233407000000.00003 / 33989999999.999996),
    落进 JSON 后既难读又显得像坏数据。金额取整到元,比值/百分比保留 2 位。"""
    stock = {"symbol": "300308", "name": "中际旭创", "snapshot_key": "300308"}
    parsed = {"2028E": {"revenue": 233_407_000_000.00003},
              "2027E": {"profit": 53_534_000_000.0}}
    disp = {"2027": {"orgs": 31, "min": 33_989_999_999.999996,
                     "max": 79_982_000_000.0, "mean": 53_534_000_000.0,
                     "spread_ratio": 2.353103854074728}}

    rec = urc.build_record(stock, parsed, disp, fetched_at="2026-08-03T15:00:00+08:00")

    assert rec["estimates"]["2028E"]["revenue_yuan"] == 233_407_000_000
    assert rec["estimates"]["2027E"]["profit_min_yuan"] == 33_990_000_000
    assert rec["estimates"]["2027E"]["spread_ratio"] == 2.35
