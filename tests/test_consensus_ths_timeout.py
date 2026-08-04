#!/usr/bin/env python3
"""test_consensus_ths_timeout.py — 同花顺调用的硬超时守卫

2026-08-04 实测事故:`update_research_consensus.py --no-email` 跑到贵州茅台 600519 时
**挂起 9.5 分钟不返回**,前 8 只已正常写出。诊断:

- 东财 F10 调用带 `timeout=25`,不是它
- akshare `stock_profit_forecast_ths` 内部是 `requests.get(url, headers=headers)`,
  **没有 timeout 参数**(实测 `'timeout' in inspect.getsource(...)` 为 False)
  → 上游不响应时会无限期阻塞

生产影响:cron 配的是 `--timeout 900`,一次挂起就会让 cron-wrapper SIGKILL,
剩余标的全部拿不到数据,且当次不产出任何告警。

这与仓库先前 `_fetch_csi_universe`(`8ca09ad`) 遇到的是同一类问题,
沿用同款守卫:daemon 线程 + `Thread.join(timeout)` 施加硬性上限。
"""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_consensus.py"
_spec = importlib.util.spec_from_file_location("update_research_consensus", _SCRIPT)
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)


def test_call_with_timeout_returns_value_when_fast():
    assert urc.call_with_timeout(lambda: "ok", timeout_s=5, label="fast") == "ok"


def test_call_with_timeout_raises_timeout_error_when_slow():
    """挂起的调用必须在限期内被放弃，而不是拖垮整个 cron。"""
    def _hang():
        time.sleep(10)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        urc.call_with_timeout(_hang, timeout_s=1, label="hang")
    assert time.monotonic() - start < 5, "未在限期内返回，守卫无效"


def test_call_with_timeout_message_names_the_call():
    """告警里要能看出是哪一步挂了，否则排查时只知道『超时了』。"""
    with pytest.raises(TimeoutError, match="600519"):
        urc.call_with_timeout(lambda: time.sleep(10), timeout_s=1, label="ths[600519]")


def test_call_with_timeout_propagates_worker_exception():
    """真实错误必须原样抛出，不能被伪装成超时。"""
    def _boom():
        raise ValueError("upstream said no")

    with pytest.raises(ValueError, match="upstream said no"):
        urc.call_with_timeout(_boom, timeout_s=5, label="boom")


def test_call_with_timeout_allows_none_result():
    """返回 None 是合法结果，不能被当成『没跑完』。"""
    assert urc.call_with_timeout(lambda: None, timeout_s=5, label="none") is None
