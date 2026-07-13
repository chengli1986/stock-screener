#!/usr/bin/env python3
"""test_fundamentals_retry.py — push2delay 瞬时超时重试单元测试

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


def _mock_response(data):
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {"data": data}
    return resp


GOOD_DATA = {
    "f116": 123456, "f162": 2131, "f163": 0, "f167": 350,
    "f173": 12.5, "f183": None, "f184": 10.1, "f185": 8.2,
    "f186": 30.0, "f187": 15.0,
}


def test_retries_after_transient_timeout_then_succeeds(monkeypatch):
    monkeypatch.setattr(spike.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None, headers=None):
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

    def always_timeout(url, params=None, timeout=None, headers=None):
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

    def fake_get(url, params=None, timeout=None, headers=None):
        calls["n"] += 1
        return _mock_response(GOOD_DATA)

    monkeypatch.setattr(spike.requests, "get", fake_get)
    rec = spike.fetch_fundamentals_one("600519.SH", "a")

    assert calls["n"] == 1
    assert rec["fetch_status"] == "ok"
