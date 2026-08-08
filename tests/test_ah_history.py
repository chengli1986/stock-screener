#!/usr/bin/env python3
"""test_ah_history.py — A/H 溢价的历史序列与分位

## 为什么只有「今天一个数」不够

页面现在显示「宁德 A 股折价 28.5%」。读者无从判断这是常态还是异常——
28.5% 可能是历史最深，也可能是一年来的中位数，两者的投资含义完全相反。

实测（2026-08-08，用同一汇率贯穿以隔离汇率变动）：宁德上市当天两地几乎平价
（A 折价 3.0%），两个半月后拉到 26.3%，此后**整整一年稳定在 18%–35% 区间**。
所以 28.5% 属于常态区间偏深的位置，而不是突发事件——这个判断只有历史序列给得出。

## 为什么用固定汇率算历史序列

溢价随时间的变化有两个来源：两地股价分化、汇率变动。用**当期汇率**贯穿全程，
可以把汇率因素固定住，让序列只反映价格分化。代价是历史点位不等于当时的真实
换算值——所以字段名标明 `fx_used`，并在页面注明口径。

（另一种做法是逐日用当日汇率，更「真实」但会把汇率波动混进曲线，
反而看不出价格分化的趋势。这里的目的是看分化，故选前者。）

## 分位数的用途

`percentile` 回答「当前溢价在历史什么位置」。0 = 历史最深折价，100 = 历史最高溢价。
宁德当前接近历史最深折价端，旭创则在中间——同样是「折价」，含义完全不同。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_snapshots.py"
_spec = importlib.util.spec_from_file_location("update_research_snapshots", _SCRIPT)
urs = importlib.util.module_from_spec(_spec)
sys.modules["update_research_snapshots"] = urs
_spec.loader.exec_module(urs)


def _series(pairs):
    """pairs: [(date, a_close, h_close), ...]"""
    return ({d: a for d, a, _ in pairs}, {d: h for d, _, h in pairs})


# ── 序列构建 ──────────────────────────────────────────────────────────────────


class TestBuildSeries:
    def test_only_common_trading_days(self):
        """两地节假日不同（如香港耶稣受难日、内地清明），只能取交集，
        否则会拿 A 股今天对 H 股昨天算溢价。"""
        a = {"2026-08-05": 100.0, "2026-08-06": 101.0, "2026-08-07": 102.0}
        h = {"2026-08-05": 120.0, "2026-08-07": 122.0}

        s = urs.build_ah_series(a, h, fx=0.86)

        assert [x["date"] for x in s] == ["2026-08-05", "2026-08-07"]

    def test_premium_uses_fixed_fx_across_history(self):
        """固定汇率贯穿，隔离汇率变动，让曲线只反映价格分化。"""
        a, h = _series([("2026-08-07", 388.07, 632.5)])

        s = urs.build_ah_series(a, h, fx=0.8586)

        assert s[0]["premium_pct"] == pytest.approx(-28.5, abs=0.2)

    def test_series_is_chronological(self):
        a, h = _series([("2026-08-07", 100.0, 100.0), ("2026-08-05", 100.0, 100.0)])

        s = urs.build_ah_series(a, h, fx=0.86)

        assert [x["date"] for x in s] == ["2026-08-05", "2026-08-07"]

    def test_empty_overlap_yields_empty(self):
        assert urs.build_ah_series({"2026-01-01": 1.0}, {"2026-02-01": 1.0}, fx=0.86) == []


# ── 统计与分位 ────────────────────────────────────────────────────────────────


class TestStats:
    _SERIES = [{"date": f"2026-01-{i:02d}", "premium_pct": p}
               for i, p in enumerate([-3.0, -10.0, -20.0, -26.0, -30.0, -28.5], start=1)]

    def test_current_is_the_last_point(self):
        st = urs.ah_stats(self._SERIES)

        assert st["current_pct"] == pytest.approx(-28.5)

    def test_min_max_span(self):
        st = urs.ah_stats(self._SERIES)

        assert st["min_pct"] == pytest.approx(-30.0)
        assert st["max_pct"] == pytest.approx(-3.0)

    def test_median(self):
        st = urs.ah_stats(self._SERIES)

        assert st["median_pct"] == pytest.approx(-23.0, abs=0.1)

    def test_percentile_places_current_within_history(self):
        """★页面要回答的核心问题：当前这个折价，在历史里算深还是浅。
        −28.5 只比 −30 高，在 6 个点里排第 2 低 → 分位约 20%（偏折价深的一端）。"""
        st = urs.ah_stats(self._SERIES)

        assert 10 <= st["percentile"] <= 35

    def test_percentile_at_extremes(self):
        low = [{"date": "d", "premium_pct": p} for p in (-5.0, -10.0, -30.0)]
        assert urs.ah_stats(low)["percentile"] == pytest.approx(0, abs=1)

        high = [{"date": "d", "premium_pct": p} for p in (-30.0, -10.0, -5.0)]
        assert urs.ah_stats(high)["percentile"] == pytest.approx(100, abs=1)

    def test_reports_observation_window(self):
        st = urs.ah_stats(self._SERIES)

        assert st["days"] == 6
        assert st["since"] == "2026-01-01"

    def test_empty_series_yields_none(self):
        assert urs.ah_stats([]) is None


# ── 溢价形成期：区分「一开始就有」和「后来拉开」 ─────────────────────────────


class TestFormation:
    def test_detects_widening_after_listing(self):
        """宁德实测：上市日 A 折价 3.0%，2.5 个月后 26.3% —— 是**后来拉开的**。
        这一条决定了解释方向：不是发行定价造成的，是上市后价格分化。"""
        s = [{"date": "2025-05-20", "premium_pct": -3.0},
             {"date": "2025-08-01", "premium_pct": -26.3},
             {"date": "2026-08-07", "premium_pct": -28.5}]

        f = urs.ah_formation(s)

        assert f["first_pct"] == pytest.approx(-3.0)
        assert f["widened"] is True

    def test_stable_since_listing_is_not_widening(self):
        s = [{"date": "2025-05-20", "premium_pct": -25.0},
             {"date": "2026-08-07", "premium_pct": -27.0}]

        assert urs.ah_formation(s)["widened"] is False

    def test_reports_extreme_dates_for_context(self):
        s = [{"date": "2025-05-20", "premium_pct": -3.0},
             {"date": "2025-09-01", "premium_pct": -35.0},
             {"date": "2026-08-07", "premium_pct": -28.5}]

        f = urs.ah_formation(s)

        assert f["min_date"] == "2025-09-01"
        assert f["max_date"] == "2025-05-20"


# ── 汇率敏感度：旭创那 3.6% 站不站得住 ───────────────────────────────────────


class TestFxSensitivity:
    def test_flags_premium_swamped_by_fx_uncertainty(self):
        """★旭创实测：汇率在 0.84~0.88 间取值，折价从 1.3% 变到 5.8% ——
        波动幅度比折价本身还大，这个数字支撑不了任何判断，必须标出来。"""
        r = urs.ah_fx_sensitivity(a_price=919.87, h_price=1110.0, fx_lo=0.84, fx_hi=0.88)

        assert r["significant"] is False
        assert r["lo_pct"] == pytest.approx(-1.3, abs=0.2)
        assert r["hi_pct"] == pytest.approx(-5.8, abs=0.2)

    def test_large_premium_survives_fx_range(self):
        """宁德：0.84~0.88 全区间都是 −27% 到 −30%，汇率解释不了。"""
        r = urs.ah_fx_sensitivity(a_price=388.07, h_price=632.5, fx_lo=0.84, fx_hi=0.88)

        assert r["significant"] is True

    def test_sanhuan_positive_premium_also_significant(self):
        r = urs.ah_fx_sensitivity(a_price=127.39, h_price=107.0, fx_lo=0.84, fx_hi=0.88)

        assert r["significant"] is True
        assert r["lo_pct"] > 30
