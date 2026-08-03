#!/usr/bin/env python3
"""test_snapshot_short_history.py — 次新股/新上市标的的历史长度守卫

触发问题(2026-08-03 实测):
1. 长鑫科技 688825 于 2026-07-27 上市,腾讯只回 6 根 K 线 → `fetch_ohlcv_data` 的
   `len(rows) < 60` 直接 raise,整个 `update_research_snapshots.py` exit 1,
   会让日 cron 每个交易日发一封告警邮件,直到累积够 60 根(约 3 个月)。
2. 更隐蔽的是智谱 02513(2026-01-08 上市,138 根):不足 252 根时旧代码
   `closes[-252:] if len(closes) >= 252 else closes` 静默退化为「全窗口涨幅」,
   却仍写进 `year_return_pct` 字段 → 线上页面把 138 个交易日的 +629.7% 标成
   「1 年涨幅」。这是标错,不是缺失。

守卫要求:数据不够就说不够(None),而不是拿短窗口冒充长窗口。
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


def _rows(n: int, start: float = 100.0, step: float = 1.0, span_days: int | None = None
          ) -> list[list[str]]:
    """构造 n 根腾讯格式 K 线:[date, open, close, high, low, volume, amount]。

    收盘价从 start 起每根 +step,便于断言涨幅。`span_days` 是首末日历日跨度,
    默认按 A 股节奏(约 242 个交易日/年)由根数推算 —— 「满一年」判据看的是
    日历跨度而不是根数(生产代码只取近 366 天,A 股一年实测仅约 241 根,
    用 252 根做门槛会把所有正常股票误判成历史不足)。
    """
    from datetime import date, timedelta

    span = span_days if span_days is not None else round(n * 365 / 242)
    end = date(2026, 8, 3)
    first = end - timedelta(days=span)
    out = []
    for i in range(n):
        close = start + i * step
        day = first + timedelta(days=round(span * i / max(n - 1, 1)))
        out.append([
            day.isoformat(), f"{close - 0.5:.2f}", f"{close:.2f}",
            f"{close + 1:.2f}", f"{close - 1:.2f}", "100000", "1000000",
        ])
    return out


# ── year_return_pct:不足 252 根必须是 None,不能拿短窗口冒充 ────────────────────


def test_year_return_is_none_when_history_shorter_than_one_year():
    """138 根 / 跨度约 208 天(智谱现状)不足一年,`year_return_pct` 必须为 None。"""
    tech = urs.compute_technicals(_rows(138))

    assert tech["year_return_pct"] is None


def test_year_return_computed_for_normal_a_share_year():
    """A 股一年实测约 241 根、跨度 365 天 —— 这是所有存量股票的常态,绝不能被误判。

    这条是防我自己:先前用「≥252 根」做门槛,真实跑下来中际旭创只有 241 根,
    会把 10 只正常股票的年涨幅全部清成 None。
    """
    tech = urs.compute_technicals(_rows(241, span_days=365))

    assert tech["year_return_pct"] == pytest.approx(round((340 / 100 - 1) * 100, 1))


def test_year_return_is_none_just_below_the_year_boundary():
    """跨度 300 天不算一年,即便根数很多。"""
    tech = urs.compute_technicals(_rows(200, span_days=300))

    assert tech["year_return_pct"] is None


def test_short_history_reports_actual_window_instead():
    """一年涨幅算不了,但「上市以来涨幅 + 实际交易日数」是真信息,应保留。"""
    tech = urs.compute_technicals(_rows(138))

    assert tech["history_days"] == 138
    assert tech["period_return_pct"] == pytest.approx(round((237 / 100 - 1) * 100, 1))


def test_full_history_still_reports_history_days():
    tech = urs.compute_technicals(_rows(241, span_days=365))

    assert tech["history_days"] == 241


# ── 6 根 K 线(长鑫科技现状)不应炸掉整个脚本 ──────────────────────────────────


def test_six_bars_does_not_raise():
    """新股只有 6 根时应返回可算的字段,而不是 raise 让 9 只正常股票一起失败。"""
    tech = urs.compute_technicals(_rows(6))

    assert tech["history_days"] == 6
    assert tech["period_return_pct"] == pytest.approx(5.0)  # 100 → 105


def test_six_bars_nulls_out_metrics_that_need_more_bars():
    tech = urs.compute_technicals(_rows(6))

    assert tech["year_return_pct"] is None
    assert tech["ma20"] is None
    assert tech["vol_60d_ann_pct"] is None
    assert tech["vol_ratio_5_60"] is None


def test_raises_only_when_no_usable_rows():
    """完全没数据才该报错 —— 那是真的抓取失败。"""
    with pytest.raises(ValueError):
        urs.compute_technicals([])


# ── ma20_slope:算不出斜率时不能默认 "down" ────────────────────────────────────


def test_ma20_slope_is_none_when_slope_not_computable():
    """旧代码 `"up" if (ma20 and ma20_5d and ...) else "down"` 会让新股凭空得到
    一个「下跌趋势」判断。算不出就是 None。"""
    tech = urs.compute_technicals(_rows(22))  # 够算 ma20(≥20),不够算 ma20_5d(≥25)

    assert tech["ma20"] is not None
    assert tech["ma20_slope"] is None


def test_ma20_slope_still_reported_when_computable():
    tech = urs.compute_technicals(_rows(30))  # 单调上涨

    assert tech["ma20_slope"] == "up"


# ── week52:不足 252 根时必须标出这不是完整 52 周 ──────────────────────────────


def test_week52_flagged_incomplete_for_short_history():
    tech = urs.compute_technicals(_rows(138))

    assert tech["week52_is_full"] is False
    assert tech["week52_high"] == pytest.approx(238.0)  # 上市以来最高,仍是真值


def test_week52_flagged_full_with_one_year_history():
    tech = urs.compute_technicals(_rows(241, span_days=365))

    assert tech["week52_is_full"] is True


# ── 落盘结构:守卫字段必须真的进 JSON,否则页面读不到 ──────────────────────────


class TestSnapshotCarriesHistoryGuard:
    """`compute_technicals` 算出来但没接进 snapshot 的话,页面照样显示错的年涨幅。"""

    _STOCK = {
        "symbol": "688825", "exchange": "SH", "name": "长鑫科技",
        "snapshot_key": "688825", "valuation_mode": "both",
        "consensus": {"2026E": {"revenue_yuan": 281.334e9, "profit_yuan": 140.256e9}},
    }
    _QUOTE = {"price_yuan": 54.73, "market_cap_yuan": 36603.91e8,
              "change_pct": 1.41, "vol_wan_shou": 500.0}
    _SHORT_OHLCV = {
        "year_return_pct": None, "period_return_pct": 5.0, "history_days": 6,
        "ma20": None, "ma20_slope": None, "vol_60d_ann_pct": None,
        "vol_ratio_5_60": None, "week52_high": 55.98, "week52_low": 50.73,
        "week52_is_full": False,
    }

    def test_snapshot_exposes_history_days_and_period_return(self):
        import unittest.mock as mock

        with mock.patch.object(urs, "fetch_quote_data", return_value=self._QUOTE), \
             mock.patch.object(urs, "fetch_ohlcv_data", return_value=self._SHORT_OHLCV):
            snap = urs.build_snapshot(self._STOCK)

        assert snap["year_return_pct"] is None
        assert snap["period_return_pct"] == pytest.approx(5.0)
        assert snap["history_days"] == 6

    def test_snapshot_technical_flags_incomplete_week52(self):
        import unittest.mock as mock

        with mock.patch.object(urs, "fetch_quote_data", return_value=self._QUOTE), \
             mock.patch.object(urs, "fetch_ohlcv_data", return_value=self._SHORT_OHLCV):
            snap = urs.build_snapshot(self._STOCK)

        assert snap["technical"]["week52_is_full"] is False
        assert snap["technical"]["ma20_slope"] is None


# ── 控制台摘要:年涨幅为 None 时不能崩,也不能假装有一年 ────────────────────────


def test_return_summary_uses_year_label_with_full_history():
    assert urs.format_return_summary({"year_return_pct": 313.0, "history_days": 321}) == "1年+313.0%"


def test_return_summary_falls_back_to_listing_period():
    """旧代码 f\"1年{yr:+.1f}%\" 对 None 会 TypeError,整只股票被算作失败。"""
    summary = urs.format_return_summary(
        {"year_return_pct": None, "period_return_pct": 5.0, "history_days": 6}
    )

    assert summary == "上市以来+5.0% (6日)"


def test_return_summary_handles_missing_everything():
    assert urs.format_return_summary({"year_return_pct": None}) == "涨幅—"
