#!/usr/bin/env python3
"""test_update_research_snapshots.py — 腾讯 qt 主源 + push2 备源 单元测试

背景:2026-06-02 起东方财富 push2 海外边缘节点对本 EC2 返回 502(国内访问正常),
行情主源切换为腾讯 qt.gtimg.cn,push2 降级为备用。

Fixture 为 2026-06-03 收盘后真实捕获的 qt.gtimg.cn 响应(GBK 解码后)。
"""

import importlib.util
import sys
import unittest.mock as mock
from pathlib import Path

import pytest
import requests

# ── 加载被测脚本(scripts/ 不是包,用 importlib 按路径加载)──────────────────────
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_snapshots.py"
_spec = importlib.util.spec_from_file_location("update_research_snapshots", _SCRIPT)
urs = importlib.util.module_from_spec(_spec)
sys.modules["update_research_snapshots"] = urs
_spec.loader.exec_module(urs)


# ── 真实 fixture(2026-06-03 捕获)─────────────────────────────────────────────
# 创业板 300308 中际旭创:现价 1275.00,涨跌 +6.98%,成交量[36]=345251(手),总市值[45]=14215.10 亿
QT_SZ300308 = (
    'v_sz300308="51~中际旭创~300308~1275.00~1191.81~1220.00~345251~187667~157584~1274.99~7'
    "~1274.91~1~1274.83~16~1274.80~1~1274.69~11~1275.00~18~1275.01~1~1275.06~1~1275.10~1"
    "~1275.11~1~~20260603155045~83.19~6.98~1320.00~1220.00~1275.00/345251/43881189297~345251"
    "~4388119~3.11~95.09~~1320.00~1220.00~8.39~14147.60~14215.10~41.05~1430.17~953.45~1.25"
    "~14~1270.99~61.97~131.65~~~1.98~4388118.9297~471.7500~37~ A~GP-A-CYB~109.36~14.72~0.11"
    "~42.01~28.64~1320.00~91.09~22.95~48.95~129.32~1109615328~1114910191~24.14~260.66"
    '~1109615328~~~1318.87~-0.19~~CNY~0~~1274.00~18~";'
)

# 科创板 688256 寒武纪:现价 1378.10,涨跌 +6.01%,成交量[36]=16559805(股),总市值[45]=8658.51 亿
QT_SH688256 = (
    'v_sh688256="1~寒武纪~688256~1378.10~1300.00~1292.30~16559805~9275043~7284762~1378.10~143'
    "~1378.08~102~1378.05~13~1378.04~2~1378.02~18~1378.35~2~1378.50~2~1379.88~2~1379.90~12"
    "~1379.92~2~~20260603155049~78.10~6.01~1428.01~1291.00~1378.10/16559805/22683725029~16559805"
    "~2268373~2.64~318.68~~1428.01~1291.00~10.54~8658.51~8658.51~70.75~1560.00~1040.00~1.24~258"
    "~1369.81~213.64~420.47~~~2.35~2268372.5029~56.5021~410~A R~GP-A-KCB~51.65~2.84~0.07~21.11"
    "~17.64~1448.01~348.44~1.15~12.61~76.03~628292969~628292969~86.58~37.72~628292969~~~245.48"
    '~-0.07~~CNY~0~___D__F__NY~1378.00~116~100";'
)


def _qt_response(text: str) -> requests.Response:
    """构造真实 requests.Response,内容为 GBK 编码(与 qt.gtimg.cn 一致)。"""
    r = requests.Response()
    r.status_code = 200
    r._content = text.encode("gbk")
    return r


# ── qt_code:腾讯代码格式 ──────────────────────────────────────────────────────


class TestQtCode:
    def test_sh_prefix(self):
        assert urs.qt_code("688256", "SH") == "sh688256"

    def test_sz_prefix(self):
        assert urs.qt_code("300308", "SZ") == "sz300308"

    def test_hk_prefix(self):
        assert urs.qt_code("02513", "HK") == "hk02513"

    def test_unknown_exchange_raises(self):
        with pytest.raises(ValueError):
            urs.qt_code("300308", "BJ")


class TestEmSecid:
    def test_sh_secid(self):
        assert urs.em_secid("688256", "SH") == "1.688256"

    def test_sz_secid(self):
        assert urs.em_secid("300308", "SZ") == "0.300308"

    def test_hk_secid(self):
        assert urs.em_secid("02513", "HK") == "116.02513"

    def test_unknown_exchange_raises(self):
        with pytest.raises(ValueError):
            urs.em_secid("300308", "BJ")


# ── fetch_qt_data:腾讯 qt 行情解析 ────────────────────────────────────────────


class TestFetchQtData:
    def test_chuangye_price_market_cap_change(self):
        """创业板 300308:现价/总市值/涨跌幅解析正确。"""
        with mock.patch.object(urs, "_get_with_retry", return_value=_qt_response(QT_SZ300308)):
            d = urs.fetch_qt_data("300308", "SZ")
        assert d["price_yuan"] == 1275.00
        assert d["market_cap_yuan"] == pytest.approx(14215.10e8)
        assert d["change_pct"] == 6.98

    def test_chuangye_volume_already_shou(self):
        """创业板成交量单位是手:345251 手 → 34.5 万手。"""
        with mock.patch.object(urs, "_get_with_retry", return_value=_qt_response(QT_SZ300308)):
            d = urs.fetch_qt_data("300308", "SZ")
        assert d["vol_wan_shou"] == 34.5

    def test_star_board_price_market_cap_change(self):
        """科创板 688256:现价/总市值/涨跌幅解析正确。"""
        with mock.patch.object(urs, "_get_with_retry", return_value=_qt_response(QT_SH688256)):
            d = urs.fetch_qt_data("688256", "SH")
        assert d["price_yuan"] == 1378.10
        assert d["market_cap_yuan"] == pytest.approx(8658.51e8)
        assert d["change_pct"] == 6.01

    def test_star_board_volume_gu_to_shou(self):
        """科创板成交量单位是股:16559805 股 → 165598 手 → 16.6 万手。"""
        with mock.patch.object(urs, "_get_with_retry", return_value=_qt_response(QT_SH688256)):
            d = urs.fetch_qt_data("688256", "SH")
        assert d["vol_wan_shou"] == 16.6

    def test_return_shape_matches_em_data(self):
        """返回字段与 fetch_em_data 一致,build_snapshot 可无缝替换。"""
        with mock.patch.object(urs, "_get_with_retry", return_value=_qt_response(QT_SZ300308)):
            d = urs.fetch_qt_data("300308", "SZ")
        assert set(d.keys()) == {"price_yuan", "market_cap_yuan", "change_pct", "vol_wan_shou"}

    def test_malformed_response_raises(self):
        with mock.patch.object(urs, "_get_with_retry", return_value=_qt_response("pv_none_match")):
            with pytest.raises(ValueError, match="malformed"):
                urs.fetch_qt_data("300308", "SZ")

    def test_too_few_fields_raises(self):
        with mock.patch.object(urs, "_get_with_retry", return_value=_qt_response('v_sz300308="51~中际旭创~300308";')):
            with pytest.raises(ValueError):
                urs.fetch_qt_data("300308", "SZ")

    def test_stock_not_found_raises(self):
        """腾讯 qt 股票不存在时返回 v_pv_none_match=0; → 抛 ValueError 触发 fallback。"""
        with mock.patch.object(urs, "_get_with_retry", return_value=_qt_response("v_pv_none_match=0;")):
            with pytest.raises(ValueError):
                urs.fetch_qt_data("999999", "SZ")

    def test_zero_price_raises(self):
        """停牌等场景现价为 0 → 必须抛错,不能写出 0 价格快照。"""
        broken = QT_SZ300308.replace("~1275.00~1191.81~", "~0.00~1191.81~", 1)
        with mock.patch.object(urs, "_get_with_retry", return_value=_qt_response(broken)):
            with pytest.raises(ValueError):
                urs.fetch_qt_data("300308", "SZ")


# ── fetch_quote_data:腾讯主 + push2 备 ───────────────────────────────────────


class TestFetchQuoteFallback:
    _QT_OK = {"price_yuan": 1275.0, "market_cap_yuan": 14215.10e8, "change_pct": 6.98, "vol_wan_shou": 34.5}
    _EM_OK = {"price_yuan": 1275.0, "market_cap_yuan": 1421510493525.0, "change_pct": 6.98, "vol_wan_shou": 34.5}

    def test_tencent_success_skips_push2(self):
        with mock.patch.object(urs, "fetch_qt_data", return_value=self._QT_OK) as m_qt, \
             mock.patch.object(urs, "fetch_em_data") as m_em:
            d = urs.fetch_quote_data("300308", "SZ")
        assert d == self._QT_OK
        m_qt.assert_called_once_with("300308", "SZ")
        m_em.assert_not_called()

    def test_tencent_failure_falls_back_to_push2(self):
        with mock.patch.object(urs, "fetch_qt_data", side_effect=requests.ConnectionError("boom")), \
             mock.patch.object(urs, "fetch_em_data", return_value=self._EM_OK) as m_em:
            d = urs.fetch_quote_data("300308", "SZ")
        assert d == self._EM_OK
        m_em.assert_called_once_with("300308", "SZ")

    def test_both_sources_fail_raises(self):
        with mock.patch.object(urs, "fetch_qt_data", side_effect=ValueError("qt down")), \
             mock.patch.object(urs, "fetch_em_data", side_effect=requests.HTTPError("502")):
            with pytest.raises(requests.HTTPError):
                urs.fetch_quote_data("300308", "SZ")


# ── build_snapshot 集成:确认走新的 fetch_quote_data ───────────────────────────


class TestBuildSnapshotUsesQuoteData:
    _STOCK = {
        "symbol": "300308",
        "exchange": "SZ",
        "name": "中际旭创",
        "snapshot_key": "300308",
        "valuation_mode": "pe",
        "consensus": {"2026E": {"profit_yuan": 32.2e9}},
    }
    _QUOTE = {"price_yuan": 1275.0, "market_cap_yuan": 14215.10e8, "change_pct": 6.98, "vol_wan_shou": 34.5}
    _OHLCV = {
        "year_return_pct": 100.0, "ma20": 1013.35, "ma20_slope": "up",
        "vol_60d_ann_pct": 59.1, "vol_ratio_5_60": 0.91,
        "week52_high": 1320.0, "week52_low": 89.85,
    }

    def test_snapshot_built_from_quote_data(self):
        """build_snapshot 调用 fetch_quote_data(而非直接 fetch_em_data)。"""
        with mock.patch.object(urs, "fetch_quote_data", return_value=self._QUOTE) as m_q, \
             mock.patch.object(urs, "fetch_ohlcv_data", return_value=self._OHLCV):
            snap = urs.build_snapshot(self._STOCK)
        m_q.assert_called_once_with("300308", "SZ")
        assert snap["price_yuan"] == 1275.0
        assert snap["market_cap_yi"] == 14215
        assert snap["change_pct"] == 6.98
        assert snap["vol_wan_shou"] == 34.5
        # PE = 市值(元) / 共识净利润(元)
        assert snap["pe_estimates"]["2026E"] == pytest.approx(14215.10e8 / 32.2e9, abs=0.1)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
