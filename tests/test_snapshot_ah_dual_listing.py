#!/usr/bin/env python3
"""test_snapshot_ah_dual_listing.py — A+H 两地上市标的的 H 股补充信息

## 事实（2026-08-08 实测，非推断）

观察池 11 只里有 3 只是 A+H 两地上市，用 akshare `stock_zh_ah_name()` 核实：
中际旭创 03308.HK、宁德时代 03750.HK、三环集团 06951.HK。

**上市主体相同**——用「市值 ÷ 股价」反算两地股本完全一致（旭创两边都是 11.70 亿股），
腾讯给 H 股算总市值时用的就是全公司股本。不存在「A 股是母公司、H 股是子公司」。

真正的差别是 **H 股只发了一小部分**：

| 标的 | 总股本 | H 股 | H 占比 | A 日成交 | H 日成交 |
|---|---|---|---|---|---|
| 中际旭创 | 11.70亿 | 0.55亿 | 4.7% | 546.7亿 | 23.6亿 |
| 宁德时代 | 46.27亿 | 2.18亿 | 4.7% | 98.8亿 | 9.6亿 |
| 三环集团 | 19.88亿 | 0.71亿 | 3.6% | 105.7亿 | 3.8亿 |

价差方向并不一致：旭创 A 折价 3.6%、宁德 A 折价 28.7%、三环 A **溢价 38.4%**。
成因未验证，故只呈现数据、不在页面上做因果解释。

## 为什么放进 snapshot 而不是单独一份数据

AH 价差必须**同时点**才有意义——拿今天的 A 股价对昨天的 H 股价算溢价是错的。
snapshot 每交易日刷新且已在抓 A 股行情，同一次跑里取 H 股是唯一能保证同时点的做法。

## 口径：跟随恒生 AH 溢价指数

`a_premium_pct = A股价 ÷ H股价折人民币 − 1`，**正值＝A 股更贵**。
与恒生 AH 股溢价指数同向（该指数 >100 表示 A 股整体溢价），避免读者反向理解。
两地原始价格一并落盘，便于自行复核。

**A 股仍是主口径**：H 股只占 4% 上下、成交额是 A 股的零头，估值分母对齐用的
仍是 A 股市值。H 股信息是**补充背景**，不是对 PE 的修正。
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


# 旭创实测值
_H_QUOTE = {"price": 1110.0, "total_cap_hkd_yuan": 1_298_405_450_000.0,
            "float_cap_hkd_yuan": 60_495_000_000.0, "turnover_hkd_yuan": 2_740_418_669.1}


# ── 溢价计算 ──────────────────────────────────────────────────────────────────


class TestPremiumCalculation:
    def test_a_discount_when_h_is_dearer(self):
        """旭创：A ¥919.87 vs H HK$1110×0.86=¥954.60 → A 折价 3.6%。"""
        r = urs.compute_ah(a_price=919.87, h=_H_QUOTE, fx=0.86, a_turnover_yi=546.7)

        assert r["a_premium_pct"] == pytest.approx(-3.6, abs=0.15)

    def test_a_premium_when_h_is_cheaper(self):
        """三环：A ¥127.39 vs H HK$107×0.86=¥92.02 → A 溢价 38.4%。"""
        h = dict(_H_QUOTE, price=107.0)
        r = urs.compute_ah(a_price=127.39, h=h, fx=0.86, a_turnover_yi=105.7)

        assert r["a_premium_pct"] == pytest.approx(38.4, abs=0.2)

    def test_sign_follows_hsi_ah_index_convention(self):
        """正值＝A 股更贵，与恒生 AH 溢价指数同向。方向搞反会让读者做出相反判断。"""
        h = dict(_H_QUOTE, price=100.0)     # H 折人民币 86 < A 100
        r = urs.compute_ah(a_price=100.0, h=h, fx=0.86, a_turnover_yi=1.0)

        assert r["a_premium_pct"] > 0

    def test_keeps_both_raw_prices_for_verification(self):
        r = urs.compute_ah(a_price=919.87, h=_H_QUOTE, fx=0.86, a_turnover_yi=546.7)

        assert r["h_price_hkd"] == pytest.approx(1110.0)
        assert r["h_price_cny"] == pytest.approx(954.6, abs=0.1)
        assert r["fx_hkd_cny"] == pytest.approx(0.86)


# ── H 股占比与流动性：判断这个价差有多少分量 ─────────────────────────────────


class TestFloatAndLiquidity:
    def test_reports_h_float_share_pct(self):
        """★这是全篇最关键的一个数：H 股只占 4.7%。
        不给这个数字，读者会把「H 股贵 3.8%」误读成「市场认为公司值更多」，
        而实际上那只是 4.7% 的筹码在另一个池子里的成交价。"""
        r = urs.compute_ah(a_price=919.87, h=_H_QUOTE, fx=0.86, a_turnover_yi=546.7)

        assert r["h_float_pct"] == pytest.approx(4.7, abs=0.2)

    def test_reports_turnover_ratio(self):
        """A 成交额 546.7 亿 vs H 23.6 亿人民币 → A 是 H 的约 23 倍。"""
        r = urs.compute_ah(a_price=919.87, h=_H_QUOTE, fx=0.86, a_turnover_yi=546.7)

        assert r["h_turnover_yi_cny"] == pytest.approx(23.6, abs=0.5)
        assert r["turnover_ratio_a_over_h"] == pytest.approx(23.2, abs=1.0)

    def test_total_shares_consistency_is_checked(self):
        """两地反算股本应一致；不一致说明口径有问题，必须标出来而不是照算溢价。"""
        r = urs.compute_ah(a_price=919.87, h=_H_QUOTE, fx=0.86, a_turnover_yi=546.7,
                           a_total_shares_yi=11.70)

        assert r["shares_consistent"] is True

    def test_inconsistent_shares_flagged(self):
        r = urs.compute_ah(a_price=919.87, h=_H_QUOTE, fx=0.86, a_turnover_yi=546.7,
                           a_total_shares_yi=5.00)

        assert r["shares_consistent"] is False


# ── 缺失容错：拿不到就别编 ────────────────────────────────────────────────────


class TestGracefulDegradation:
    def test_missing_fx_yields_no_premium(self):
        """与港股 PS 换算同一口径：没有汇率宁可不给，也不能按 1:1 算。"""
        r = urs.compute_ah(a_price=919.87, h=_H_QUOTE, fx=None, a_turnover_yi=546.7)

        assert r["a_premium_pct"] is None
        assert r["h_price_hkd"] == pytest.approx(1110.0)   # 原始值仍保留

    def test_missing_h_quote_returns_none(self):
        assert urs.compute_ah(a_price=919.87, h=None, fx=0.86, a_turnover_yi=546.7) is None

    def test_zero_h_price_yields_no_block_at_all(self):
        """H 股停牌或数据异常（价格为 0）时整块不可用——
        页面什么都不显示，好过显示一行「HK$0.0」让人以为股价归零了。
        （初版这条我期望返回残缺 dict，实现返回 None 更合理，改的是期望。）"""
        h = dict(_H_QUOTE, price=0.0)

        assert urs.compute_ah(a_price=919.87, h=h, fx=0.86, a_turnover_yi=1.0) is None


# ── 接线：只有配了 h_share 的标的才抓，且不能拖垮主流程 ──────────────────────


class TestWiredIntoSnapshot:
    _STOCK_AH = {"symbol": "300308", "exchange": "SZ", "name": "中际旭创",
                 "snapshot_key": "300308", "valuation_mode": "pe",
                 "h_share": {"code": "03308", "tencent": "hk03308"},
                 "consensus": {"2026E": {"profit_yuan": 3.09e10}}}
    _STOCK_PLAIN = {"symbol": "688256", "exchange": "SH", "name": "寒武纪",
                    "snapshot_key": "688256", "valuation_mode": "pe",
                    "consensus": {"2026E": {"profit_yuan": 5.0e9}}}
    _QUOTE = {"price_yuan": 919.87, "market_cap_yuan": 1.076004e12,
              "change_pct": 1.0, "vol_wan_shou": 57.2, "turnover_yuan": 5.467e10}
    _OHLCV = {"year_return_pct": 300.0, "period_return_pct": 300.0, "history_days": 241,
              "ma20": 900.0, "ma20_slope": "up", "vol_60d_ann_pct": 80.0,
              "vol_ratio_5_60": 1.1, "week52_high": 1416.88, "week52_low": 195.8,
              "week52_is_full": True}

    def _build(self, stock, tmp_path, h_quote=_H_QUOTE, fx=0.86):
        import unittest.mock as mock
        with mock.patch.object(urs, "fetch_quote_data", return_value=self._QUOTE), \
             mock.patch.object(urs, "fetch_ohlcv_data", return_value=self._OHLCV), \
             mock.patch.object(urs, "fetch_h_quote", return_value=h_quote), \
             mock.patch.object(urs, "fetch_fx_rate", return_value=fx), \
             mock.patch.object(urs, "DATA_DIR", tmp_path):
            return urs.build_snapshot(stock)

    def test_ah_block_present_for_dual_listed(self, tmp_path):
        snap = self._build(self._STOCK_AH, tmp_path)

        assert snap["ah"]["h_code"] == "03308"
        assert snap["ah"]["a_premium_pct"] == pytest.approx(-3.6, abs=0.2)

    def test_no_ah_block_for_single_listed(self, tmp_path):
        """没配 h_share 的标的不该多出一个空壳字段，页面会据此判断要不要显示。"""
        snap = self._build(self._STOCK_PLAIN, tmp_path)

        assert snap.get("ah") is None

    def test_valuation_still_uses_a_share_cap(self, tmp_path):
        """★H 股信息是补充背景，**不能**改变估值分母对齐——
        A 股占 95% 股本、成交额是 H 的 23 倍，它才是主口径。"""
        snap = self._build(self._STOCK_AH, tmp_path)

        assert snap["market_cap_yi"] == 10760
        assert snap["pe_estimates"]["2026E"] == pytest.approx(round(1.076004e12 / 3.09e10, 1))

    def test_h_fetch_failure_does_not_break_snapshot(self, tmp_path):
        """H 股是加分项；抓不到时主快照必须照常产出。"""
        import unittest.mock as mock
        with mock.patch.object(urs, "fetch_quote_data", return_value=self._QUOTE), \
             mock.patch.object(urs, "fetch_ohlcv_data", return_value=self._OHLCV), \
             mock.patch.object(urs, "fetch_h_quote", side_effect=RuntimeError("qt down")), \
             mock.patch.object(urs, "fetch_fx_rate", return_value=0.86), \
             mock.patch.object(urs, "DATA_DIR", tmp_path):
            snap = urs.build_snapshot(self._STOCK_AH)

        assert snap["market_cap_yi"] == 10760
        assert snap.get("ah") is None


# ── 成交额字段：曾经静默丢失过 ────────────────────────────────────────────────


class TestTurnoverFieldPlumbing:
    """★接线时发现 `fetch_qt_data` 根本不返回 turnover_yuan，
    而 build_snapshot 里 `quote.get("turnover_yuan")` 永远取到 None ——
    成交额对比会**静默丢失**：页面照常显示 AH 价差，只是少了「A 股成交额是
    H 股 23 倍」这个判断分量的关键背景，而没有任何报错。

    这类「取一个不存在的键」的 bug 不会抛异常，只会让功能悄悄少一半。
    """

    def test_qt_returns_turnover(self):
        import unittest.mock as mock

        # 腾讯 A 股真实响应片段（旭创 2026-08-08），[37]=成交额（万元）
        fields = ["1", "中际旭创", "300308"] + ["0"] * 85
        fields[3] = "919.87"
        fields[32] = "1.5"
        fields[36] = "571892"
        fields[37] = "5467064"      # 万元 → 546.7 亿
        fields[45] = "10760.04"
        body = "v_sz300308=\"" + "~".join(fields) + "\";"

        class _R:
            text = body
            encoding = "gbk"

        with mock.patch.object(urs, "_get_with_retry", return_value=_R()):
            q = urs.fetch_qt_data("300308", "SZ")

        assert q["turnover_yuan"] == pytest.approx(5467064 * 1e4)

    def test_turnover_reaches_the_ah_block(self, tmp_path):
        """字段存在还不够，得真的流到 ah 块里。"""
        snap = TestWiredIntoSnapshot()._build(
            TestWiredIntoSnapshot._STOCK_AH, tmp_path)

        assert snap["ah"]["a_turnover_yi_cny"] == pytest.approx(546.7, abs=0.5)
        assert snap["ah"]["turnover_ratio_a_over_h"] is not None
