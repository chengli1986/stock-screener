#!/usr/bin/env python3
"""test_snapshot_fx_conversion.py — 港股 PS/PE 的币种换算

触发问题（2026-08-04 实测）：智谱 02513.HK 的市值报价是 **HKD**，而机构一致预期的
营收/净利是 **CNY**（yfinance `financialCurrency=CNY`, `currency=HKD`）。
`update_research_snapshots.py` 原实现直接 `市值 / 营收`，两个币种相除 → PS 系统性偏高
约 16%（HKDCNY 实测 0.8604）。

A 股无此问题（报价与财务同为 CNY），故换算只在注册表显式声明 `consensus_currency`
且与报价币种不同时才发生 —— 不给 A 股引入任何行为变化。
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


# ── quote_currency：报价币种由交易所决定 ──────────────────────────────────────


@pytest.mark.parametrize("exchange,expected", [("SH", "CNY"), ("SZ", "CNY"), ("HK", "HKD")])
def test_quote_currency_by_exchange(exchange, expected):
    assert urs.quote_currency(exchange) == expected


# ── convert_market_cap：把市值换算到一致预期所用的币种 ────────────────────────


def test_no_conversion_when_currencies_match():
    """A 股：报价与财务同为 CNY，必须原样返回，不引入任何浮点漂移。"""
    cap = 1_055_700_000_000.0

    assert urs.convert_market_cap(cap, "CNY", "CNY", fx_rate=0.8604) == cap


def test_converts_hkd_market_cap_to_cny():
    """智谱：4805 亿 HKD × 0.8604 = 4134 亿 CNY。"""
    cap_hkd = 480_500_000_000.0

    got = urs.convert_market_cap(cap_hkd, "HKD", "CNY", fx_rate=0.8604)

    assert got == pytest.approx(413_422_200_000.0, rel=1e-6)


def test_missing_fx_rate_raises_rather_than_silently_skipping():
    """拿不到汇率时宁可失败 —— 静默跳过换算会产出偏高 16% 的 PS 且无人察觉。"""
    with pytest.raises(ValueError, match="汇率"):
        urs.convert_market_cap(1e11, "HKD", "CNY", fx_rate=None)


def test_zero_or_negative_fx_rate_rejected():
    for bad in (0, -0.5):
        with pytest.raises(ValueError):
            urs.convert_market_cap(1e11, "HKD", "CNY", fx_rate=bad)


# ── build_snapshot 集成：PS 必须按换算后市值计算 ──────────────────────────────


class TestHkSnapshotUsesConvertedCap:
    _STOCK = {
        "symbol": "02513", "exchange": "HK", "name": "智谱", "snapshot_key": "02513",
        "valuation_mode": "ps",
        "consensus_currency": "CNY",
        "consensus": {
            "2026E": {"label": "2026E", "revenue_yuan": 3_730_110_510},
            "2027E": {"label": "2027E", "revenue_yuan": 9_538_914_610},
        },
    }
    _QUOTE = {"price_yuan": 1024.0, "market_cap_yuan": 480_500_000_000.0,
              "change_pct": 2.0, "vol_wan_shou": 500.0}
    _OHLCV = {"year_return_pct": None, "period_return_pct": 600.0, "history_days": 139,
              "ma20": 1300.0, "ma20_slope": "down", "vol_60d_ann_pct": 200.0,
              "vol_ratio_5_60": 1.5, "week52_high": 2980.0, "week52_low": 116.1,
              "week52_is_full": False}

    def _build(self):
        import unittest.mock as mock
        with mock.patch.object(urs, "fetch_quote_data", return_value=self._QUOTE), \
             mock.patch.object(urs, "fetch_ohlcv_data", return_value=self._OHLCV), \
             mock.patch.object(urs, "fetch_fx_rate", return_value=0.8604):
            return urs.build_snapshot(self._STOCK)

    def test_ps_uses_converted_market_cap(self):
        """4134 亿 CNY / 37.30 亿 = 110.8x（而非未换算的 128.8x）。"""
        snap = self._build()

        assert snap["ps_estimates"]["2026E"] == pytest.approx(110.8, abs=0.5)

    def test_ps_2027_also_converted(self):
        snap = self._build()

        assert snap["ps_estimates"]["2027E"] == pytest.approx(43.3, abs=0.5)

    def test_snapshot_records_fx_for_reproducibility(self):
        """汇率与币种必须落盘，否则事后无法复算这个 PS 是怎么来的。"""
        snap = self._build()

        assert snap["fx_rate"] == pytest.approx(0.8604)
        assert snap["quote_currency"] == "HKD"
        assert snap["consensus_currency"] == "CNY"

    def test_market_cap_yi_stays_in_quote_currency(self):
        """展示用市值仍是港元口径 —— 换算只用于估值分母对齐，不该改变行情展示。"""
        snap = self._build()

        assert snap["market_cap_yi"] == 4805


class TestAShareSnapshotUnaffected:
    """A 股不得因本次改动产生任何行为变化。"""

    _STOCK = {
        "symbol": "300308", "exchange": "SZ", "name": "中际旭创", "snapshot_key": "300308",
        "valuation_mode": "pe",
        "consensus": {"2026E": {"label": "2026E", "profit_yuan": 30_394_000_000}},
    }
    _QUOTE = {"price_yuan": 1026.0, "market_cap_yuan": 1_200_100_000_000.0,
              "change_pct": 13.7, "vol_wan_shou": 80.0}
    _OHLCV = {"year_return_pct": 335.7, "period_return_pct": 335.7, "history_days": 241,
              "ma20": 1060.0, "ma20_slope": "down", "vol_60d_ann_pct": 84.0,
              "vol_ratio_5_60": 1.3, "week52_high": 1416.88, "week52_low": 195.8,
              "week52_is_full": True}

    def test_a_share_pe_unchanged_and_no_fx_fetch(self, tmp_path):
        # DATA_DIR 指向空目录：本用例验证注册表兜底路径，不应受真实 consensus.json 影响
        import unittest.mock as mock
        with mock.patch.object(urs, "fetch_quote_data", return_value=self._QUOTE), \
             mock.patch.object(urs, "fetch_ohlcv_data", return_value=self._OHLCV), \
             mock.patch.object(urs, "DATA_DIR", tmp_path), \
             mock.patch.object(urs, "fetch_fx_rate") as m_fx:
            snap = urs.build_snapshot(self._STOCK)

        m_fx.assert_not_called(), "A 股不该触发汇率抓取"
        assert snap["pe_estimates"]["2026E"] == pytest.approx(1_200_100_000_000 / 30_394_000_000, abs=0.1)
        assert snap.get("fx_rate") is None
