#!/usr/bin/env python3
"""test_csi_universe_retry.py — 中证指数官网瞬时挂起重试单元测试

背景: _fetch_csi_universe 用 daemon 线程 + join 给 akshare CSI 300/500 拉取加
30s 硬超时(akshare 不暴露 timeout),但无重试。2026-07-20 10:00 BJT canary 撞上
csindex.com.cn 瞬时挂起(>30s),事后手动实测 csindex.com.cn 1.5s 恢复,确认非代码
bug。与 fundamentals push2delay 瞬时超时(见 test_fundamentals_retry.py)同一类,
按既定阈值补重试退避。
"""

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "phase0_spike.py"
_spec = importlib.util.spec_from_file_location("phase0_spike", _SCRIPT)
spike = importlib.util.module_from_spec(_spec)
sys.modules["phase0_spike"] = spike
_spec.loader.exec_module(spike)


def test_retries_after_transient_hang_then_succeeds(monkeypatch):
    monkeypatch.setattr(spike.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_once(timeout_s):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("simulated upstream hang")
        return ("df300", "df500")

    monkeypatch.setattr(spike, "_fetch_csi_once", fake_once)
    df300, df500 = spike._fetch_csi_universe(timeout_s=30)

    assert calls["n"] == 3
    assert (df300, df500) == ("df300", "df500")


def test_gives_up_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(spike.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_hang(timeout_s):
        calls["n"] += 1
        raise TimeoutError("simulated upstream hang")

    monkeypatch.setattr(spike, "_fetch_csi_once", always_hang)

    try:
        spike._fetch_csi_universe(timeout_s=30)
        assert False, "expected TimeoutError after exhausting retries"
    except TimeoutError:
        pass

    assert calls["n"] == 1 + len(spike.CSI_RETRY_BACKOFFS)


def test_no_retry_needed_on_first_success(monkeypatch):
    monkeypatch.setattr(spike.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_once(timeout_s):
        calls["n"] += 1
        return ("df300", "df500")

    monkeypatch.setattr(spike, "_fetch_csi_once", fake_once)
    spike._fetch_csi_universe(timeout_s=30)

    assert calls["n"] == 1
