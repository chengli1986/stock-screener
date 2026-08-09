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

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["mean"] == pytest.approx(150.0)
    assert out["weighted_mean"] < 150.0, "加权均值未向新预测靠拢"


def test_robust_stats_reports_outlier_details():
    """离群者要能看出是谁、差多少 —— 只给个 count 没法判断。"""
    recs = _recs([(81.0, "2026-07-01"), (84.0, "2026-07-01"), (86.0, "2026-07-01"),
                  (87.0, "2026-07-01"), (88.0, "2026-07-01"), (128.0, "2026-07-01")])

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert len(out["outliers"]) == 1
    assert out["outliers"][0]["value"] == 128.0
    assert "券商5" in out["outliers"][0]["org"]


def test_robust_stats_reports_staleness():
    """最老一份的年龄是判断这组预期还能不能用的关键。"""
    recs = _recs([(100.0, "2026-08-01"), (110.0, "2026-04-02")])

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["oldest_age_days"] == 124
    assert out["newest_age_days"] == 3


def test_robust_stats_gives_median_even_for_a_single_record():
    """★2026-08-09 用户定不设薄覆盖阈值：单条记录的中位数就是它自己，
    如实给出即可；原先留 None 并置 insufficient_samples 的分支已删。"""
    out = urc.robust_stats({"2026E": _recs([(100.0, "2026-08-01")])},
                           today=TODAY)["2026E"]

    assert out["median"] == 100
    assert "insufficient_samples" not in out
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

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert len(out["outliers"]) == 1
    assert out["outliers"][0]["deviation_pct"] == pytest.approx(44.5, abs=1.0)


# ── 薄覆盖标的：显式回退到简单算术平均 ────────────────────────────────────────
#
# 用户（前卖方分析师）指示：长光华芯(3家)/风华高科(2家)/长鑫科技(2家) 这类覆盖，
# 加权、中位数、离群检测在该样本量上全都无意义，**简单算术平均更可行**。
# 故不再让下游自己去猜该用哪个统计量——直接落盘 `preferred_stat` / `preferred_value`。


def test_preferred_stat_is_trimmed_mean_when_coverage_is_thick():
    """2026-08-05 起采用口径由中位数改为截尾均值（用户选定）。
    中位数仍照常计算并落盘，只是不再是 preferred。"""
    recs = [{"org": f"券商{i}", "published": "2026-07-01", "value": v}
            for i, v in enumerate([100.0, 102.0, 104.0, 106.0, 108.0, 110.0])]

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["preferred_stat"] == "trimmed_mean"
    assert out["preferred_value"] == out["trimmed_mean"]
    assert out["median"] == pytest.approx(105.0), "中位数仍应正确计算"


def test_thin_coverage_keeps_the_same_statistic():
    """★不再按样本量切换统计量：截尾对 2 个样本本就退化成算术均值，
    无需特判，下游拿到的口径始终一致。"""
    recs = [{"org": "券商A", "published": "2026-07-01", "value": 100.0},
            {"org": "券商B", "published": "2026-01-01", "value": 200.0}]

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["preferred_stat"] == "trimmed_mean"
    assert out["preferred_value"] == 150.0, "两样本的截尾均值等于算术均值"


def test_preferred_value_never_uses_recency_weighting():
    """采用值始终是截尾均值，不是时间加权均值（加权仅作参考落盘）。"""
    recs = [{"org": "券商A", "published": "2026-08-01", "value": 100.0},
            {"org": "券商B", "published": "2025-01-01", "value": 200.0}]

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["preferred_value"] == 150.0
    assert out["weighted_mean"] != out["preferred_value"]


def test_reason_is_always_robust_now():
    """薄覆盖不再是一种「理由」——统计量不因家数而变。"""
    recs = [{"org": "券商A", "published": "2026-07-01", "value": 100.0}]

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["preferred_reason"] == "robust"


def test_thick_coverage_reason_is_robust():
    recs = [{"org": f"券商{i}", "published": "2026-07-01", "value": 100.0 + i}
            for i in range(6)]

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["preferred_reason"] == "robust"


# ── 截尾均值（用户选定的估值分母口径）────────────────────────────────────────
#
# 用户判断：算买点价位时用「剔除极值后的平均值」比纯均值或中位数都合理——
# 保留大部分样本信息，同时不被个别离谱预测带偏。
#
# 规则：n≥5 时两端各剔除 max(1, floor(n×10%)) 个；n<5 已属薄覆盖，不截尾（直接均值）。


def test_trimmed_mean_drops_both_extremes():
    """10 个样本剔除两端各 1 个 → 只有中间 8 个参与平均。"""
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 1000.0]

    # 剔除 10 与 1000 后，剩 20..90 的均值 = 55
    assert urc.trimmed_mean(values, trim_pct=10.0) == pytest.approx(55.0)


def test_trimmed_mean_resists_a_single_wild_forecast():
    """一个离谱高值不该把买点抬上去 —— 这正是不用纯均值的理由。"""
    values = [100.0, 102.0, 104.0, 106.0, 108.0, 500.0]

    plain = sum(values) / len(values)
    trimmed = urc.trimmed_mean(values, trim_pct=10.0)

    assert plain > 160
    assert trimmed == pytest.approx(105.0)


def test_trimmed_mean_scales_trim_count_with_sample_size():
    """20 个样本按 10% 两端各剔 2 个。"""
    values = [1.0, 2.0] + [10.0] * 16 + [1000.0, 2000.0]

    assert urc.trimmed_mean(values, trim_pct=10.0) == pytest.approx(10.0)


def test_trimmed_mean_falls_back_to_plain_mean_when_thin():
    """n<5 已是薄覆盖，再截尾会只剩一两个点。"""
    values = [10.0, 20.0, 90.0]

    assert urc.trimmed_mean(values, trim_pct=10.0) == pytest.approx(40.0)


def test_trimmed_mean_handles_empty():
    assert urc.trimmed_mean([], trim_pct=10.0) is None


def test_robust_stats_exposes_trimmed_mean():
    recs = [{"org": f"券商{i}", "published": "2026-07-01", "value": v}
            for i, v in enumerate([100.0, 102.0, 104.0, 106.0, 108.0, 500.0])]

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["trimmed_mean"] == pytest.approx(105.0)


def test_preferred_stat_uses_trimmed_mean_when_coverage_is_thick():
    """★口径切换：覆盖足够时估值分母采用截尾均值（原为中位数）。"""
    recs = [{"org": f"券商{i}", "published": "2026-07-01", "value": v}
            for i, v in enumerate([100.0, 102.0, 104.0, 106.0, 108.0, 500.0])]

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["preferred_stat"] == "trimmed_mean"
    assert out["preferred_value"] == pytest.approx(105.0)


def test_thin_coverage_result_equals_plain_mean():
    """薄覆盖下截尾均值数值上等于算术平均——结果不变，只是不再改标签。"""
    recs = [{"org": "A", "published": "2026-07-01", "value": 100.0},
            {"org": "B", "published": "2026-01-01", "value": 200.0}]

    out = urc.robust_stats({"2026E": recs}, today=TODAY)["2026E"]

    assert out["preferred_stat"] == "trimmed_mean"
    assert out["preferred_value"] == 150.0
