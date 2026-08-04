#!/usr/bin/env python3
"""test_consensus_robust_stats.py — 新鲜度加权 + MAD 离群标记

用户（前卖方分析师）指出：分析师预测不满足正态假设——每个人锚定同一份公司指引、
看得到彼此的数、职业风险不对称（跟随共识错了没事，独立错了要走人），
结果是**紧密聚类 + 少数离群**，不是正态。拟合正态会把 herding 当成高置信度。

故不拟合分布，改用稳健统计：

1. **新鲜度加权**：东财 `ycmx` 带 `PUBLISH_DATE`。旭创最老一份发布于 2026-04-02
   （四个月前），对周期股已是废数据，不该与上周的预测等权。用半衰期指数衰减。
2. **MAD 离群标记**：`|x − median| > k × MAD`。**标记而不删除** —— 离群者可能是
   唯一看对的人，删掉就永远看不见；标出来让人自己判断。
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_consensus.py"
_spec = importlib.util.spec_from_file_location("update_research_consensus", _SCRIPT)
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)

TODAY = date(2026, 8, 4)


# ── 新鲜度 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("published,expected", [
    ("2026-08-04", 0),
    ("2026-08-01", 3),
    ("2026-04-02", 124),
])
def test_estimate_age_days(published, expected):
    assert urc.estimate_age_days(published, TODAY) == expected


@pytest.mark.parametrize("bad", ["", None, "不是日期", "2026-13-45"])
def test_estimate_age_days_returns_none_on_unparseable(bad):
    assert urc.estimate_age_days(bad, TODAY) is None


def test_recency_weight_is_one_for_today():
    assert urc.recency_weight(0, half_life_days=90) == pytest.approx(1.0)


def test_recency_weight_halves_at_half_life():
    assert urc.recency_weight(90, half_life_days=90) == pytest.approx(0.5)


def test_recency_weight_decays_monotonically():
    ws = [urc.recency_weight(d, half_life_days=90) for d in (0, 30, 90, 180, 365)]
    assert ws == sorted(ws, reverse=True)


def test_recency_weight_unknown_age_gets_penalised_not_dropped():
    """日期缺失不等于数据无效，但也不该享受满权重。"""
    w = urc.recency_weight(None, half_life_days=90)
    assert 0 < w < 1


# ── MAD 离群 ──────────────────────────────────────────────────────────────────


def test_mad_of_symmetric_sample():
    # median=30，偏差 [20,10,0,10,20] → MAD = 10
    assert urc.median_abs_deviation([10.0, 20.0, 30.0, 40.0, 50.0]) == 10.0


def test_flag_outliers_marks_the_far_one():
    """寒武纪形态：多数挤在一起 + 少数拖尾。"""
    values = [81.0, 84.0, 86.0, 87.0, 88.0, 90.0, 128.0]

    flags = urc.flag_outliers(values, k=3.0)

    assert flags[-1] is True
    assert not any(flags[:-1])


def test_flag_outliers_returns_all_false_when_mad_is_zero():
    """全部相同 → MAD=0，不能因除零把所有点都判成离群。"""
    assert urc.flag_outliers([50.0] * 5, k=3.0) == [False] * 5


def test_flag_outliers_needs_minimum_sample():
    """n=2 时中位数与 MAD 都没有意义，一律不标。"""
    assert urc.flag_outliers([10.0, 900.0], k=3.0) == [False, False]


# ── robust_stats:整合 ─────────────────────────────────────────────────────────


def _recs(items):
    """items: [(value, published)]"""
    return [{"org": f"券商{i}", "published": p, "value": v} for i, (v, p) in enumerate(items)]


def test_robust_stats_weighted_mean_favours_recent():
    """两个预测，新的那个应把加权均值拉向自己。"""
    recs = _recs([(100.0, "2026-08-01"), (200.0, "2025-08-01")])

    out = urc.robust_stats({"2026E": recs}, today=TODAY, min_samples=2)["2026E"]

    assert out["mean"] == pytest.approx(150.0)
    assert out["weighted_mean"] < 150.0, "加权均值未向新预测靠拢"


def test_robust_stats_reports_outlier_details():
    """离群者要能看出是谁、差多少 —— 只给个 count 没法判断。"""
    recs = _recs([(81.0, "2026-07-01"), (84.0, "2026-07-01"), (86.0, "2026-07-01"),
                  (87.0, "2026-07-01"), (88.0, "2026-07-01"), (128.0, "2026-07-01")])

    out = urc.robust_stats({"2026E": recs}, today=TODAY, min_samples=5)["2026E"]

    assert len(out["outliers"]) == 1
    assert out["outliers"][0]["value"] == 128.0
    assert "券商5" in out["outliers"][0]["org"]


def test_robust_stats_reports_staleness():
    """最老一份的年龄是判断这组预期还能不能用的关键。"""
    recs = _recs([(100.0, "2026-08-01"), (110.0, "2026-04-02")])

    out = urc.robust_stats({"2026E": recs}, today=TODAY, min_samples=2)["2026E"]

    assert out["oldest_age_days"] == 124
    assert out["newest_age_days"] == 3


def test_robust_stats_withholds_median_below_min_samples():
    """沿用既有约定：样本不足不给中位数，但极值与加权均值仍给。"""
    out = urc.robust_stats({"2026E": _recs([(100.0, "2026-08-01")])},
                           today=TODAY, min_samples=5)["2026E"]

    assert out["median"] is None
    assert out["insufficient_samples"] is True
    assert out["max"] == 100
    assert out["weighted_mean"] == 100


def test_robust_stats_skips_years_with_no_values():
    assert urc.robust_stats({"2026E": []}, today=TODAY) == {}


# ── 双门槛：统计异常 + 经济重要 ───────────────────────────────────────────────
#
# 首跑实测暴露单用 MAD 的缺陷：MAD 衡量的是相对**该标的自身共识紧密度**的异常，
# 共识越紧、同样的绝对偏离得到的 MAD 倍数越大。结果茅台（46 家高度一致）
# 3.3×MAD 只对应 −5% 偏离也被标记，而寒武纪 7×MAD 对应 +43%。
# 统计上异常 ≠ 经济上重要 → 必须同时要求偏离中位数达到一个经济门槛。


def test_outlier_requires_both_statistical_and_economic_significance():
    """紧密共识里的小偏离：MAD 倍数很大，但经济上无关紧要 —— 不该标。"""
    values = [850.0, 852.0, 851.0, 853.0, 849.0, 894.0]  # 最后一个仅高出中位数约 5%

    flags = urc.flag_outliers(values, k=3.0, min_deviation_pct=20.0)

    assert not any(flags), "紧密共识中 5% 的偏离被误标为离群"


def test_outlier_flagged_when_economically_material():
    """寒武纪形态：+43% 偏离，两个门槛都过。"""
    values = [81.0, 84.0, 86.0, 87.0, 88.0, 125.0]

    flags = urc.flag_outliers(values, k=3.0, min_deviation_pct=20.0)

    assert flags[-1] is True


def test_outlier_not_flagged_on_economic_alone():
    """离散度本来就大时，单个 40% 偏离并不异常 —— 统计门槛应挡住。"""
    values = [50.0, 70.0, 100.0, 130.0, 150.0, 140.0]

    flags = urc.flag_outliers(values, k=3.0, min_deviation_pct=20.0)

    assert not any(flags)


def test_robust_stats_records_deviation_pct_for_review():
    """标出来的项要能看到经济偏离幅度，不能只有 MAD 倍数。"""
    recs = [{"org": f"券商{i}", "published": "2026-07-01", "value": v}
            for i, v in enumerate([81.0, 84.0, 86.0, 87.0, 88.0, 125.0])]

    out = urc.robust_stats({"2026E": recs}, today=TODAY, min_samples=5)["2026E"]

    assert len(out["outliers"]) == 1
    assert out["outliers"][0]["deviation_pct"] == pytest.approx(44.5, abs=1.0)
