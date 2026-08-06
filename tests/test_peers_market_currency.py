#!/usr/bin/env python3
"""test_peers_market_currency.py — 同业市值表推广到 11 只的前置：跨市场币种换算

## 为什么必须先做这个

`update_research_peers_market.py` 原来只服务 3 只，同业几乎都是 A 股，
所以「不换算」这个缺陷一直没暴露。但它**已经错了**：寒武纪（A 股）的同业里有
壁仞科技（港股），页面把 987（亿港元）和海光的 6,765（亿人民币）并列在同一列，
且渲染代码只写「亿」不带币种（`cambricon-688256.html` L1278）。

推广到 11 只之后这个问题会放大到不可接受：各页已有的同业清单里包含
- 盛科 → Broadcom / Marvell（**美股**，腾讯返回 19,900 = 亿美元）
- 源杰 → Lumentum（美股）、三菱电机（**日股**，119,552 = 亿日元）
- 长鑫 → 三星电子 / SK 海力士（**韩股**，15,135,928 = 亿韩元）
- 风华 / 三环 → 村田、TDK、京瓷、太阳诱电（日股）、三星电机（韩股）

腾讯 qt 的 `[45]` 字段是**本币计价的「亿」**，跨市场直接并列比较是错的。
（2026-08-06 实测确认：美股/港股/A股/韩股/日股均有数据，**台股 tw2330 无数据**。）

## 设计取舍

换算到 **CNY** 统一口径 —— 观察池标的以 A 股为主，读者的锚点是人民币。
换算后同时保留原币值与汇率，页面要能说清「这是换算值」而不是伪装成原生数据。

**拿不到汇率时不静默跳过**：宁可让该 peer 缺失，也不能把 19,900 亿美元
当成 19,900 亿人民币显示 —— 这与 8-04 港股 PS 换算的处置口径一致。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_peers_market.py"
_spec = importlib.util.spec_from_file_location("update_research_peers_market", _SCRIPT)
upm = importlib.util.module_from_spec(_spec)
sys.modules["update_research_peers_market"] = upm
_spec.loader.exec_module(upm)


# ── 换算本身 ──────────────────────────────────────────────────────────────────


class TestConvertMarketCap:
    _FX = {"USD": 7.18, "HKD": 0.9142, "JPY": 0.0478, "KRW": 0.00521}

    def test_cny_passes_through_untouched(self):
        assert upm.convert_cap(16357.94, "CNY", self._FX) == pytest.approx(16357.94)

    def test_usd_is_converted(self):
        """Broadcom 实测 19,900 亿美元 → 约 14.29 万亿人民币。"""
        assert upm.convert_cap(19900.0, "USD", self._FX) == round(19900 * 7.18)

    def test_hkd_is_converted(self):
        """壁仞 987 亿港元 —— 当前页面直接当人民币并列，这条钉死修复。"""
        assert upm.convert_cap(987.0, "HKD", self._FX) == round(987 * 0.9142)

    def test_jpy_is_converted(self):
        """三菱电机 119,552 亿日元。不换算会显示成人民币 11.9 万亿（虚高 20 倍）。"""
        assert upm.convert_cap(119552.0, "JPY", self._FX) == round(119552 * 0.0478)

    def test_krw_is_converted(self):
        """三星 15,135,928 亿韩元。不换算会显示成 1,513 万亿人民币（虚高 190 倍）。"""
        got = upm.convert_cap(15135928.0, "KRW", self._FX)
        assert got == round(15135928 * 0.00521)

    def test_missing_rate_raises_rather_than_silently_passing_through(self):
        """★没有汇率时**不能**当作 1:1 —— 那正是把 19,900 亿美元印成人民币的路径。"""
        with pytest.raises(ValueError):
            upm.convert_cap(19900.0, "USD", {})

    def test_none_cap_stays_none(self):
        assert upm.convert_cap(None, "USD", self._FX) is None


# ── 落盘结构：换算值与原值都要留 ──────────────────────────────────────────────


class TestRecordKeepsProvenance:
    def test_records_both_native_and_cny(self):
        rec = upm.build_peer_record(
            {"name": "Broadcom", "code": "AVGO", "tencent": "usAVGO",
             "market": "美股", "currency": "USD"},
            price=418.28, cap_native=19900.0, year_return_pct=52.1,
            fx={"USD": 7.18},
        )

        assert rec["market_cap_yi_native"] == pytest.approx(19900.0)
        assert rec["market_cap_yi"] == round(19900 * 7.18)
        assert rec["currency"] == "USD"

    def test_keeps_tencent_code_for_page_cell_matching(self):
        """★页面就地水合按 data-pm-cap="sz000858" 匹配单元格，靠的是 tencent 码。

        首版没落这个字段，页面 JS 拿 `p.code`（展示格式「000858.SZ」）去匹配，
        静默匹配不上——脚注更新了、数字没变，看起来像「刷新过了」，实则没有。
        """
        rec = upm.build_peer_record(
            {"name": "五粮液", "code": "000858.SZ", "tencent": "sz000858",
             "market": "深主板", "currency": "CNY"},
            price=74.48, cap_native=2891.0, year_return_pct=-37.1, fx={},
        )

        assert rec["tencent"] == "sz000858"
        assert rec["code"] == "000858.SZ"     # 展示格式与匹配键是两回事

    def test_records_the_rate_actually_used(self):
        """页面要能说清「按 7.18 换算」，否则读者无法复核。"""
        rec = upm.build_peer_record(
            {"name": "Broadcom", "code": "AVGO", "tencent": "usAVGO",
             "market": "美股", "currency": "USD"},
            price=418.28, cap_native=19900.0, year_return_pct=None, fx={"USD": 7.18},
        )

        assert rec["fx_to_cny"] == pytest.approx(7.18)

    def test_cny_peer_has_rate_one_and_no_conversion_noise(self):
        rec = upm.build_peer_record(
            {"name": "海光信息", "code": "688041", "tencent": "sh688041",
             "market": "科创板", "currency": "CNY"},
            price=291.0, cap_native=6765.0, year_return_pct=114.1, fx={},
        )

        assert rec["market_cap_yi"] == pytest.approx(6765.0)
        assert rec["fx_to_cny"] == 1.0

    def test_missing_rate_marks_peer_unconverted_instead_of_lying(self):
        """拿不到汇率：市值置 None + 记原因，宁可缺失也不能错。"""
        rec = upm.build_peer_record(
            {"name": "Broadcom", "code": "AVGO", "tencent": "usAVGO",
             "market": "美股", "currency": "USD"},
            price=418.28, cap_native=19900.0, year_return_pct=None, fx={},
        )

        assert rec["market_cap_yi"] is None
        assert rec["market_cap_yi_native"] == pytest.approx(19900.0)
        assert "USD" in (rec.get("note") or "")


# ── 市场前缀推导 ──────────────────────────────────────────────────────────────


class TestCurrencyFromTencentCode:
    @pytest.mark.parametrize("code,ccy", [
        ("sh600519", "CNY"), ("sz000858", "CNY"),
        ("hk00700", "HKD"), ("usAVGO", "USD"),
        ("jp6503", "JPY"), ("kr005930", "KRW"),
    ])
    def test_infers_currency(self, code, ccy):
        assert upm.currency_of(code) == ccy

    def test_unknown_prefix_is_explicit_not_guessed_as_cny(self):
        """台股 tw2330 腾讯实测无数据；真接进来时不能默认按人民币处理。"""
        assert upm.currency_of("tw2330") is None


# ── 海外股年涨幅：腾讯 K 线不给，得换源 ───────────────────────────────────────


class TestOverseasYearReturn:
    """★实测（2026-08-06）：腾讯 fqkline 对海外股**只返回 1–2 根** K 线
    （`usAVGO` 2 根、`jp6503` 1 根、`kr005930` 1 根），而 A 股/港股返回 261 根。

    首轮全池跑完 11 只，**所有海外同业的「1年涨幅」全是 None** —— 表格里
    A 股同业有涨幅、海外同业一片空白。这不是抓取失败，是接口本身不供数。
    改走 yfinance（脚本已用它取汇率，不引入新依赖）。
    """

    @pytest.mark.parametrize("tencent,expected", [
        ("usAVGO", "AVGO"),          # 美股去后缀
        ("usTSM", "TSM"),
        ("jp6503", "6503.T"),        # 东证
        ("kr005930", "005930.KS"),   # 韩交所
    ])
    def test_maps_tencent_code_to_yfinance_ticker(self, tencent, expected):
        assert upm.yf_ticker_of(tencent) == expected

    @pytest.mark.parametrize("tencent", ["sh600519", "sz000858", "hk00700"])
    def test_a_share_and_hk_stay_on_tencent(self, tencent):
        """A 股/港股腾讯给足 261 根，没有理由换源。"""
        assert upm.yf_ticker_of(tencent) is None

    def test_year_return_falls_back_for_overseas(self, monkeypatch):
        monkeypatch.setattr(upm, "_tencent_year_return", lambda c: None)
        monkeypatch.setattr(upm, "_yf_year_return", lambda t: 52.1)

        assert upm.fetch_year_return("usAVGO") == pytest.approx(52.1)

    def test_no_fallback_attempted_for_a_share(self, monkeypatch):
        """A 股腾讯真返回 None 时是「数据不足」，不该再去 yfinance 白等。"""
        called = {"n": 0}
        monkeypatch.setattr(upm, "_tencent_year_return", lambda c: None)
        monkeypatch.setattr(upm, "_yf_year_return",
                            lambda t: called.__setitem__("n", called["n"] + 1))

        assert upm.fetch_year_return("sh600519") is None
        assert called["n"] == 0


# ── 精度：换算值不能带 12 位小数 ──────────────────────────────────────────────


def test_converted_cap_is_rounded():
    """首轮实测打出 `¥4332.922333717346 亿` —— 汇率乘出来的浮点尾巴要收掉。"""
    rec = upm.build_peer_record(
        {"name": "Coherent", "code": "COHR.US", "tencent": "usCOHR",
         "market": "美股", "currency": "USD"},
        price=328.22, cap_native=642.0, year_return_pct=None, fx={"USD": 6.74910},
    )

    assert rec["market_cap_yi"] == round(rec["market_cap_yi"])
