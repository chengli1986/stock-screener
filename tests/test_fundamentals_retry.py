#!/usr/bin/env python3
"""test_fundamentals_retry.py — 基本面请求瞬时超时重试单元测试（2026-10-08 起源为腾讯 qt）

背景: fetch_fundamentals_one 单次 requests.get(timeout=15) 无重试,2026-07-05/
07-08/07-13 canary 三次撞上上游瞬时挂起(1-2 只 symbol 15.24s 超时),事后手动
重放均 <2s 恢复,确认非代码 bug。第 3 次复现后按既定阈值补重试退避。
"""

import importlib.util
import sys
import unittest.mock as mock
from pathlib import Path

import pytest
import requests

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "phase0_spike.py"
_spec = importlib.util.spec_from_file_location("phase0_spike", _SCRIPT)
spike = importlib.util.module_from_spec(_spec)
sys.modules["phase0_spike"] = spike
_spec.loader.exec_module(spike)


def _mock_response(fields):
    resp = mock.Mock()
    resp.status_code = 200
    resp.content = ('v_sh600519="' + "~".join(fields) + '";').encode("gbk")
    return resp


# Tencent qt payload: [39] PE(TTM)  [45] 总市值(亿)  [46] PB
GOOD_DATA = ["0"] * 88
GOOD_DATA[1] = "贵州茅台"
GOOD_DATA[39] = "21.31"
GOOD_DATA[45] = "15698.40"
GOOD_DATA[46] = "6.25"


def test_retries_after_transient_timeout_then_succeeds(monkeypatch):
    monkeypatch.setattr(spike.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_get(url, timeout=None, headers=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.Timeout("simulated upstream hang")
        return _mock_response(GOOD_DATA)

    monkeypatch.setattr(spike.requests, "get", fake_get)
    rec = spike.fetch_fundamentals_one("600519.SH", "a")

    assert calls["n"] == 3
    assert rec["fetch_status"] == "ok"
    assert rec["pe_ttm"]["value"] == 21.31


def test_gives_up_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(spike.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_timeout(url, timeout=None, headers=None):
        calls["n"] += 1
        raise requests.Timeout("simulated upstream hang")

    monkeypatch.setattr(spike.requests, "get", always_timeout)
    rec = spike.fetch_fundamentals_one("600519.SH", "a")

    assert calls["n"] == 1 + len(spike.FUND_RETRY_BACKOFFS)
    assert rec["fetch_status"] == "error"
    assert rec["error_type"] == "timeout"


def test_no_retry_needed_on_first_success(monkeypatch):
    monkeypatch.setattr(spike.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_get(url, timeout=None, headers=None):
        calls["n"] += 1
        return _mock_response(GOOD_DATA)

    monkeypatch.setattr(spike.requests, "get", fake_get)
    rec = spike.fetch_fundamentals_one("600519.SH", "a")

    assert calls["n"] == 1
    assert rec["fetch_status"] == "ok"


def test_tencent_payload_maps_valuation_fields(monkeypatch):
    monkeypatch.setattr(spike.requests, "get", lambda url, timeout=None, headers=None: _mock_response(GOOD_DATA))
    rec = spike.fetch_fundamentals_one("600519.SH", "a")

    assert rec["fetch_status"] == "ok"
    assert rec["pe_ttm"]["value"] == 21.31
    assert rec["pb"]["value"] == 6.25
    assert rec["market_cap"]["value"] == 15698.40 * 1e8
    # 腾讯不提供盈利/增长字段 → missing_expected，不能算 fetch_error
    assert rec["roe_ttm"]["field_status"] == "missing_expected"


def test_hk_pb_not_sourced(monkeypatch):
    monkeypatch.setattr(spike.requests, "get", lambda url, timeout=None, headers=None: _mock_response(GOOD_DATA))
    rec = spike.fetch_fundamentals_one("0700.HK", "hk")

    assert rec["fetch_status"] == "ok"
    assert rec["pb"]["value"] is None
    assert rec["pb"]["field_status"] == "missing_expected"


def test_tencent_symbol_codes():
    assert spike.tencent_symbol("600519.SH") == "sh600519"
    assert spike.tencent_symbol("000858.SZ") == "sz000858"
    assert spike.tencent_symbol("700.HK") == "hk00700"


def test_unknown_symbol_payload_is_error(monkeypatch):
    resp = mock.Mock(status_code=200, content=b'v_pv_none_match="1";')
    monkeypatch.setattr(spike.requests, "get", lambda url, timeout=None, headers=None: resp)
    rec = spike.fetch_fundamentals_one("600519.SH", "a")

    assert rec["fetch_status"] == "error"
    assert rec["error_type"] == "empty_response"
