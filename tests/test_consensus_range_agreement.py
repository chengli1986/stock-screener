#!/usr/bin/env python3
"""test_consensus_range_agreement.py — 两源预测区间的重叠度

**动机（2026-08-05 深挖寒武纪背离时发现的盲点）**：
现有统计层对每个源**内部**算了离散度（MAD、离群、spread_ratio），
但对**源之间的样本构成差异**没有任何度量。

寒武纪实测：同花顺比东财多收录 3–5 家机构，而这几家在远期极度乐观——
2028E 多出机构的隐含均值是共同部分的 1.76 倍。均值差异因此随年限单调放大
（−1.4% → −9.0% → −11.5%）。

但**区间早就分开了**：2026E 同花顺 max 88.8 vs 东财 max 70.3，
那时均值差异还只有 −1.4%、被判 CONFIRMED。
→ 区间重叠度是比均值差异**更早**的预警信号。

仅适用于 A 股：港股两源指标不重叠（yfinance 给营收、ETNet 给净利），
区间无从比较，与 cross_check 的限制相同。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_consensus.py"
_spec = importlib.util.spec_from_file_location("update_research_consensus", _SCRIPT)
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)


# ── 基本重叠度 ────────────────────────────────────────────────────────────────


def test_identical_ranges_fully_agree():
    r = urc.range_agreement((10.0, 20.0), (10.0, 20.0))

    assert r["overlap_pct"] == pytest.approx(100.0)


def test_disjoint_ranges_have_zero_overlap():
    r = urc.range_agreement((10.0, 20.0), (30.0, 40.0))

    assert r["overlap_pct"] == 0.0


def test_cambricon_2027_real_case():
    """实测：同花顺 [76.1, 261.3] vs 东财 [81.3, 127.9]。

    交集宽 46.6、并集宽 185.2 → 25.2%（我口算成 22% 是错的，故在此钉死）。
    """
    r = urc.range_agreement((76.1e8, 261.3e8), (81.3e8, 127.9e8))

    assert r["overlap_pct"] == pytest.approx(25.2, abs=0.3)


def test_detects_containment():
    """东财区间完全落在同花顺区间内 —— 说明同花顺收录了更极端的机构。"""
    r = urc.range_agreement((76.1, 261.3), (81.3, 127.9))

    assert r["secondary_contained"] is True


def test_containment_false_when_partial_overlap():
    r = urc.range_agreement((10.0, 20.0), (15.0, 30.0))

    assert r["secondary_contained"] is False


def test_span_ratio_shows_who_is_wider():
    """次源宽度 / 主源宽度。寒武纪 2027E = 46.6/185.2 ≈ 0.25。"""
    r = urc.range_agreement((76.1, 261.3), (81.3, 127.9))

    assert r["span_ratio"] == pytest.approx(0.25, abs=0.02)


# ── 预警分级 ──────────────────────────────────────────────────────────────────


def test_flags_low_overlap_as_sample_divergence():
    """重叠 <50% → 两源看的根本不是同一批机构，均值可比性存疑。"""
    r = urc.range_agreement((76.1, 261.3), (81.3, 127.9))

    assert r["verdict"] == "SAMPLE_DIVERGENT"


def test_high_overlap_is_aligned():
    r = urc.range_agreement((100.0, 200.0), (105.0, 195.0))

    assert r["verdict"] == "ALIGNED"


def test_cambricon_2026_would_warn_before_mean_diverges():
    """★本指标的价值所在：2026E 均值只差 −1.4%（判 CONFIRMED），
    但区间已明显分开（同花顺 [34.0, 88.8] vs 东财 [47.0, 70.3]）。"""
    r = urc.range_agreement((34.0e8, 88.8e8), (47.0e8, 70.3e8))

    assert r["overlap_pct"] < 50
    assert r["verdict"] == "SAMPLE_DIVERGENT"


# ── 缺失容错 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("a,b", [
    (None, (1.0, 2.0)),
    ((1.0, 2.0), None),
    ((None, 2.0), (1.0, 2.0)),
    ((1.0, None), (1.0, 2.0)),
])
def test_missing_bounds_yield_unavailable(a, b):
    r = urc.range_agreement(a, b)

    assert r["verdict"] == "UNAVAILABLE"
    assert r["overlap_pct"] is None


def test_zero_width_primary_does_not_divide_by_zero():
    """只有一家机构时 min==max，宽度为 0。"""
    r = urc.range_agreement((50.0, 50.0), (40.0, 60.0))

    assert r["verdict"] != "ALIGNED"
    assert r["overlap_pct"] is not None
