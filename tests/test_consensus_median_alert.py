#!/usr/bin/env python3
"""test_consensus_median_alert.py — 逐家机构中位数 + 两源背离告警

两项改进（2026-08-03，用户确认优先级）：

1. **中位数替代均值**：同花顺给的是简单算术均值，会被极端值拉动
   （旭创 2027E 净利 31 家机构的 max/min 达 2.35 倍）。东财 `ycmx` 有逐家明细，
   可自算中位数。**但样本量必须够** —— 实测长鑫仅 2 家、长光华芯 3 家，
   对这类标的算中位数没有统计意义，须如实标为样本不足而不是给一个假精度的数。

2. **背离告警**：`cross_check` 的 DIVERGENT 此前只打印在 cron 日志里，
   没人会去翻。改为发邮件，让下一次「寒武纪式背离」立刻可见。
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


def _ycmx(n: int, values: list[float], year2: int = 2026) -> list[dict]:
    """构造 n 条 ycmx 行；values 为各家 YEAR2 的归母净利。"""
    return [
        {
            "ORG_NAME_ABBR": f"机构{i}", "PUBLISH_DATE": f"2026-0{(i % 6) + 1}-15 00:00:00",
            "YEAR1": 2025, "YEAR_MARK1": "A", "PARENT_NETPROFIT1": 1e9,
            "YEAR2": year2, "YEAR_MARK2": "E", "PARENT_NETPROFIT2": v,
            "YEAR3": year2 + 1, "YEAR_MARK3": "E", "PARENT_NETPROFIT3": v * 1.5,
            "YEAR4": year2 + 2, "YEAR_MARK4": "E", "PARENT_NETPROFIT4": None,
        }
        for i, v in enumerate(values[:n])
    ]


# ── parse_em_broker_estimates:按年份归集逐家预测 ──────────────────────────────


def test_broker_estimates_group_by_forecast_year():
    """年份轴必须按 YEAR_MARK 动态映射，不能写死 YEAR2=2026。"""
    rows = _ycmx(3, [100.0, 200.0, 300.0])

    out = urc.parse_em_broker_estimates(rows)

    assert sorted(out) == ["2026E", "2027E"]
    assert out["2026E"] == [100.0, 200.0, 300.0]
    assert out["2027E"] == [150.0, 300.0, 450.0]


def test_broker_estimates_skip_actual_years():
    """YEAR1 是 'A'（已披露实际值），不能混进预测样本。"""
    out = urc.parse_em_broker_estimates(_ycmx(2, [100.0, 200.0]))

    assert "2025E" not in out and "2025A" not in out


def test_broker_estimates_drop_missing_values():
    rows = _ycmx(3, [100.0, 200.0, 300.0])
    rows[1]["PARENT_NETPROFIT2"] = None

    assert urc.parse_em_broker_estimates(rows)["2026E"] == [100.0, 300.0]


def test_broker_estimates_handles_empty():
    assert urc.parse_em_broker_estimates([]) == {}
    assert urc.parse_em_broker_estimates(None) == {}


# ── broker_stats:中位数与样本量守卫 ───────────────────────────────────────────


def test_broker_stats_computes_median_with_enough_samples():
    stats = urc.broker_stats({"2026E": [100.0, 200.0, 300.0, 400.0, 500.0]}, min_samples=5)

    assert stats["2026E"]["median"] == 300.0
    assert stats["2026E"]["count"] == 5
    assert stats["2026E"]["insufficient_samples"] is False


def test_broker_stats_median_of_even_count_averages_middle_two():
    stats = urc.broker_stats({"2026E": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]}, min_samples=5)

    assert stats["2026E"]["median"] == 35.0


def test_broker_stats_withholds_median_when_samples_too_few():
    """长鑫仅 2 家、长光 3 家 —— 给中位数是假精度，必须留 None 并标明。"""
    stats = urc.broker_stats({"2026E": [100.0, 500.0]}, min_samples=5)

    assert stats["2026E"]["median"] is None
    assert stats["2026E"]["insufficient_samples"] is True
    assert stats["2026E"]["count"] == 2


def test_broker_stats_still_reports_range_when_samples_too_few():
    """样本不够不代表数据无用 —— min/max 仍是真信息。"""
    stats = urc.broker_stats({"2026E": [100.0, 500.0]}, min_samples=5)

    assert stats["2026E"]["min"] == 100.0
    assert stats["2026E"]["max"] == 500.0


def test_broker_stats_median_differs_from_mean_under_skew():
    """这条说明中位数为什么值得做：一个极端值就能把均值拉偏 40%+。"""
    values = [100.0, 110.0, 120.0, 130.0, 900.0]

    stats = urc.broker_stats({"2026E": values}, min_samples=5)

    assert stats["2026E"]["median"] == 120.0
    assert stats["2026E"]["mean"] == pytest.approx(272.0)


# ── 告警正文 ──────────────────────────────────────────────────────────────────


def test_alert_html_lists_divergent_items():
    rows = [{"name": "寒武纪", "year": "2027E", "field": "revenue",
             "primary": 31_350_000_000, "secondary": 28_110_000_000, "diff_pct": -10.3}]

    html = urc.build_divergence_alert_html(rows, fetched_at="2026-08-03T20:00:00+08:00")

    assert "寒武纪" in html and "2027E" in html and "-10.3" in html
    assert "<html" in html.lower()


def test_alert_html_escapes_names():
    """标的名进 HTML 前必须转义 —— 注册表是可编辑的输入。"""
    rows = [{"name": "<script>x</script>", "year": "2026E", "field": "profit",
             "primary": 1e9, "secondary": 2e9, "diff_pct": 100.0}]

    html = urc.build_divergence_alert_html(rows, fetched_at="2026-08-03T20:00:00+08:00")

    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_alert_html_empty_when_nothing_diverges():
    assert urc.build_divergence_alert_html([], fetched_at="2026-08-03T20:00:00+08:00") is None
