#!/usr/bin/env python3
"""test_price_alert_conditions.py — 买点提醒的新触发逻辑

## 为什么重写

旧实现是**每只股票写死一个价位**（旭创「跌到 800 元提醒」）。诊断下来它失效有两层：

1. **价位是化石**：800 元是用当时的盈利预测 × 28 倍算的；预测已从 285 亿涨到 308 亿，
   同样 28 倍对应的价位应该更高。分母一变，阈值就错。
2. **阈值定得过深**：实测大部分标的还要跌 20–76% 才触发。
   **前几天全池普跌 30–50% 时一条都没触发** —— 那正是最该提醒的时刻，系统是哑的。
   两个月里只触发过一次（盛科 2026-06-04）。

## 新逻辑：捕捉「杀估值不杀盈利」

这是我们在这轮回撤里实证发现的信号——旭创跌 33% 而 2027E 预期被**上调 14%**
（错位，值得看）；源杰跌 35% 但 2027E 营收预期被**下调 24%**（基本面恶化，不该买）。

四条同时满足才触发：
- ①a 距 52 周高回撤 ≥25%（有折价）
- ①b 高点距今 ≤120 天（是**近期急跌**而非长期阴跌——用户提出，脱离时间的回撤无意义）
- ③ PEG < 1.0（2027E 口径，增速撑得住估值）
- ④ 估值可信度门禁：视界内无 DIVERGENT / UNVERIFIED / SAMPLE_DIVERGENT / thin_coverage

② 修正方向（预期是否被下调）需要历史快照，当前只有一个时间点，**待 9-01 第二次
抓取后补上**。在此之前不假装有这一条。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research_price_alert.py"
_spec = importlib.util.spec_from_file_location("research_price_alert", _SCRIPT)
rpa = importlib.util.module_from_spec(_spec)
sys.modules["research_price_alert"] = rpa
_spec.loader.exec_module(rpa)


def _snapshot(price=945.0, high=1416.88, age=44, full=True, cap_yi=11000):
    return {
        "name": "中际旭创", "price_yuan": price, "market_cap_yi": cap_yi,
        "technical": {"week52_high": high, "week52_high_age_days": age,
                      "week52_is_full": full},
    }


def _consensus(p26=3.08e10, p27=5.40e10, verdict="CONFIRMED",
               range_verdict="ALIGNED", thin=False):
    years = ("2026E", "2027E")
    return {
        "estimates": {y: {"profit_yuan": v} for y, v in zip(years, (p26, p27))},
        "broker_stats": {y: {"preferred_value": v, "insufficient_samples": thin}
                         for y, v in zip(years, (p26, p27))},
        "cross_check": {y: {"profit": {"verdict": verdict}} for y in years},
        "range_agreement": {y: {"verdict": range_verdict} for y in years},
    }


# ── ① 回撤：幅度 + 时效 ───────────────────────────────────────────────────────


def test_triggers_on_recent_deep_drawdown():
    ok, _ = rpa.evaluate(_snapshot(), _consensus())

    assert ok is True


def test_shallow_drawdown_does_not_trigger():
    """宁德只跌 13% —— 没折价就不是买点，即便 PEG 很好。"""
    ok, reasons = rpa.evaluate(_snapshot(price=405.0, high=468.75, age=90), _consensus())

    assert ok is False
    assert any("回撤" in r for r in reasons)


def test_old_peak_does_not_trigger_even_if_deep():
    """★用户提出的核心约束：11 个月阴跌 30% 是趋势性下跌，不是错杀。"""
    ok, reasons = rpa.evaluate(_snapshot(age=330), _consensus())

    assert ok is False
    assert any("天" in r for r in reasons)


def test_peak_age_exactly_at_boundary_still_triggers():
    ok, _ = rpa.evaluate(_snapshot(age=120), _consensus())

    assert ok is True


def test_missing_peak_age_does_not_trigger():
    """算不出高点时效就不该假设它是近期的。"""
    ok, reasons = rpa.evaluate(_snapshot(age=None), _consensus())

    assert ok is False


# ── ③ PEG ────────────────────────────────────────────────────────────────────


def test_high_peg_does_not_trigger():
    """三环真实数据：市值 2405 亿、2026E 35.7 亿 → 2027E 45.1 亿（+26%），
    2027E PE 53.3x → PEG 2.05。跌 29% 但增速撑不住估值。"""
    ok, reasons = rpa.evaluate(
        _snapshot(price=128.33, high=180.35, age=37, cap_yi=2405),
        _consensus(p26=3.57e9, p27=4.51e9))

    assert ok is False
    assert any("PEG" in r for r in reasons)


def test_negative_growth_does_not_trigger():
    """预期在萎缩时 PEG 无意义，不能因为算不出就放行。"""
    ok, reasons = rpa.evaluate(_snapshot(), _consensus(p26=5.0e10, p27=4.0e10))

    assert ok is False


def test_uses_preferred_value_not_raw_mean():
    """估值分母口径是截尾均值（broker_stats.preferred_value），不是 estimates 均值。"""
    c = _consensus()
    c["broker_stats"]["2027E"]["preferred_value"] = 1.0e10   # 远低 → PEG 变大
    ok, reasons = rpa.evaluate(_snapshot(), c)

    assert ok is False, "未采用 preferred_value 口径"


# ── ④ 可信度门禁 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kw,expect", [
    ({"verdict": "DIVERGENT"}, "背离"),
    ({"verdict": "UNVERIFIED"}, "未验证"),
    ({"range_verdict": "SAMPLE_DIVERGENT"}, "样本"),
    ({"thin": True}, "覆盖"),
])
def test_credibility_gate_blocks(kw, expect):
    """估值数据不可信时一律不提醒 —— 基于坏分母的买点是徒劳的（用户前提）。"""
    ok, reasons = rpa.evaluate(_snapshot(), _consensus(**kw))

    assert ok is False
    assert any(expect in r for r in reasons)


# ── 次新股 ───────────────────────────────────────────────────────────────────


def test_incomplete_52w_window_is_flagged_not_silently_used():
    """长鑫/智谱的「52 周高」其实是上市以来高点，须标注而非当作真 52 周。"""
    ok, reasons = rpa.evaluate(_snapshot(full=False), _consensus())

    assert any("未满一年" in r or "参考" in r for r in reasons)


# ── 输出 ─────────────────────────────────────────────────────────────────────


def test_reasons_explain_every_failed_condition():
    """一次列全，便于判断离触发还差多少，而不是只报第一个不满足项。"""
    ok, reasons = rpa.evaluate(
        _snapshot(price=405.0, high=468.75, age=330),
        _consensus(p26=5.0e10, p27=5.2e10, verdict="DIVERGENT"))

    assert ok is False
    assert len(reasons) >= 3
