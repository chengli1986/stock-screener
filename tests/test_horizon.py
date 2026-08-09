#!/usr/bin/env python3
"""test_horizon.py — 预测视界的单一定义与滚动

## 用户的判断（2026-08-05 首次，2026-08-09 重申）

> 「从现在看 27 年一整年，差不多，28 年太过于久远；而且现在世界格局变化极大，
>   A 股又不是一个强有效市场，看一年半已经足够了。」
> 「一致预期看到 2027 年底就够了，2028 年有点遥远。」

## 为什么要单独抽出来

2026-08-09 盘点发现视界**写死在 4 个地方**：
`research_price_alert.py:66`、`update_research_consensus.py:1203`、
同文件 `:1555`、以及 `docs-site/js/consensus-quality.js:46`——
四份 `("2026E", "2027E")` 各写各的。

两个后果：
1. **2027 年一到全都错**。到时视界该是 2027E/2028E，而代码仍死盯 2026E——
   那时 2026 已是实际值，页面会拿一个已公布的年份当预测展示。
2. 改一处漏三处。这套管线已经因为「同一概念多处定义」栽过跟头
   （覆盖机构数 count vs orgs）。

## 滚动规则：当前年 + 次年

- 2026-08 → `2026E, 2027E`（到 2027 年底，约 1.4 年）＝用户此刻要的
- 2027-03 → `2027E, 2028E`
视界长度在 1~2 年间浮动，符合「一年半左右」的本意，且不需要每年手工改。
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("_horizon", _ROOT / "scripts" / "_horizon.py")
hz = importlib.util.module_from_spec(_spec)
sys.modules["_horizon"] = hz
_spec.loader.exec_module(hz)


class TestHorizonYears:
    def test_current_and_next_year(self):
        """2026-08 → 到 2027 年底，正是用户此刻说的视界。"""
        assert hz.horizon_years(date(2026, 8, 9)) == ("2026E", "2027E")

    def test_rolls_with_the_calendar(self):
        """★写死的版本 2027 年一到就错：那时 2026 已是实际值。"""
        assert hz.horizon_years(date(2027, 3, 1)) == ("2027E", "2028E")

    def test_year_boundary(self):
        assert hz.horizon_years(date(2026, 12, 31)) == ("2026E", "2027E")
        assert hz.horizon_years(date(2027, 1, 1)) == ("2027E", "2028E")

    def test_membership_helper(self):
        assert hz.in_horizon("2027E", date(2026, 8, 9)) is True
        assert hz.in_horizon("2028E", date(2026, 8, 9)) is False

    def test_actual_years_never_in_horizon(self):
        """实际值年份（A 后缀）不是预测，永远不该进视界。"""
        assert hz.in_horizon("2025A", date(2026, 8, 9)) is False
        assert hz.in_horizon("2026A", date(2026, 8, 9)) is False

    def test_filter_keeps_order(self):
        got = hz.filter_horizon(["2028E", "2026E", "2025A", "2027E"], date(2026, 8, 9))

        assert got == ["2026E", "2027E"]

    def test_filter_tolerates_missing(self):
        assert hz.filter_horizon(["2027E"], date(2026, 8, 9)) == ["2027E"]

    def test_filter_empty(self):
        assert hz.filter_horizon([], date(2026, 8, 9)) == []


class TestSnapshotRespectsHorizon:
    """snapshot 此前照单全收，长鑫/茅台都算出并落盘了 2028E 倍数。"""

    def test_out_of_horizon_multiples_not_computed(self, tmp_path):
        import unittest.mock as mock
        s = importlib.util.spec_from_file_location(
            "urs", _ROOT / "scripts" / "update_research_snapshots.py")
        urs = importlib.util.module_from_spec(s); sys.modules["urs"] = urs
        s.loader.exec_module(urs)

        stock = {"symbol": "600519", "exchange": "SH", "name": "贵州茅台",
                 "snapshot_key": "600519", "valuation_mode": "pe",
                 "consensus": {"2026E": {"profit_yuan": 8.6e10},
                               "2027E": {"profit_yuan": 9.1e10},
                               "2028E": {"profit_yuan": 9.6e10}}}
        quote = {"price_yuan": 1309.0, "market_cap_yuan": 1.6366e12,
                 "change_pct": 0.1, "vol_wan_shou": 2.5, "turnover_yuan": 1e10}
        ohlcv = {"year_return_pct": -4.5, "period_return_pct": -4.5, "history_days": 241,
                 "ma20": 1300.0, "ma20_slope": "down", "vol_60d_ann_pct": 20.0,
                 "vol_ratio_5_60": 1.0, "week52_high": 1600.0, "week52_low": 1200.0,
                 "week52_is_full": True}
        with mock.patch.object(urs, "fetch_quote_data", return_value=quote), \
             mock.patch.object(urs, "fetch_ohlcv_data", return_value=ohlcv), \
             mock.patch.object(urs, "DATA_DIR", tmp_path):
            snap = urs.build_snapshot(stock)

        assert "2028E" not in snap["pe_estimates"]
        assert set(snap["pe_estimates"]) == {"2026E", "2027E"}
