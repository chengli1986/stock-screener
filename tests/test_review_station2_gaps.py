#!/usr/bin/env python3
"""test_review_station2_gaps.py — 第二站自审查出的三个缺漏

2026-08-08 自审（用户要求「确认第二站有没有缺漏」）发现，昨天做的三项改造各留了一个口子。

## ① 陈旧阈值与新频率不匹配 —— 加密的意义被抵消

`CONSENSUS_MAX_DAYS = 40` 是按月频设的，而 4/5/8/9 月已改为**每周一**抓。
加密期连续失败 5 次（35 天）都不会告警——加密本是为了让分母跟上半年报季的
预期调整，结果它可以静默停摆一个多月。

单纯把阈值改成 10 天会在**加密月初误报**：4 月第一个周一可能是 4-06，
在那之前最近一次抓取是 3-01（月频档），距今 31 天 > 10 天，但这完全正常。

所以判据改为**「按 cron 规则本应触发几次」**：上次抓取之后本应跑 ≥2 次
却一次都没更新，才是真异常。这个语义不依赖月份边界，也不用为频率变化重调阈值。

## ② revision momentum 漏了营收维度 —— 而动机案例恰恰是营收

`revision_momentum()` 只读 `profit`。但当初论证这个功能必要性的案例是
「源杰跌 35% 但 2027E **营收**预期被下调 24%」——实现算不出自己的论据。
`revenue` 其实一直存在历史文件里，只是没被读。

补上还顺带解决亏损标的：智谱 profit 为负（−44 亿）算不出百分比，
但它的 revenue 是正的，完全能算。

## ③ 历史写入失败是静默的

`write_and_deploy()` 把 history 写入包在 try/except 里（理由是「历史是加分项，
不该拖垮当期数据」），失败只打 WARN、脚本仍 exit 0 → cron-wrapper 不告警。

后果最坏的地方在于**无法区分**：`revision_momentum` 会一直返回 `insufficient`，
而这个状态和「刚开始积累、点数还不够」长得一模一样。几个月后想看修正方向，
才发现一个点都没攒下。故补一条检查：history 的最新观测必须跟得上 consensus。
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rdh = _load("research_data_health", "research_data_health.py")
urc = _load("update_research_consensus", "update_research_consensus.py")


# ── ① 按「本应触发几次」判断，而不是天数 ─────────────────────────────────────


class TestExpectedRunsBasedStaleness:
    """cron 规则：每月 1 日 + 4/5/8/9 月每周一。"""

    def test_counts_monthly_run(self):
        """7-01 抓过，8-08 检查：8-01 月度档 + 8-03 周一（8 月是加密月）= 2 次。

        （初版这条我写成 1，漏算了加密月的周一——实现是对的，期望值错了。）
        """
        assert urc.expected_runs_between(date(2026, 7, 1), date(2026, 8, 8)) == 2

    def test_counts_weekly_runs_in_reporting_season(self):
        """8 月是加密月：8-03、8-10、8-17 三个周一 + 8-01 月度档 = 4 次。"""
        assert urc.expected_runs_between(date(2026, 7, 31), date(2026, 8, 17)) == 4

    def test_no_weekly_runs_outside_reporting_season(self):
        """7 月不是加密月，只有 7-01 月度档一次。"""
        assert urc.expected_runs_between(date(2026, 6, 30), date(2026, 7, 31)) == 1

    def test_same_day_yields_zero(self):
        assert urc.expected_runs_between(date(2026, 8, 8), date(2026, 8, 8)) == 0

    def test_month_start_boundary_is_not_a_false_alarm(self):
        """★单纯用 10 天阈值会误报的那个场景：4 月第一个周一是 4-06，
        在此之前最近一次抓取是 3-01（月频档），距今 31 天——但本应触发的
        只有 4-01 一次，属正常，不该告警。"""
        n = urc.expected_runs_between(date(2026, 3, 1), date(2026, 4, 3))

        assert n == 1   # 仅 4-01；4-06 那个周一还没到

    def test_five_missed_weekly_runs_is_an_anomaly(self):
        """加密期连续失败 5 次 —— 旧的 40 天阈值恰好放过它。"""
        n = urc.expected_runs_between(date(2026, 8, 3), date(2026, 9, 7))

        assert n >= 5


class TestHealthUsesExpectedRuns:
    def test_one_missed_run_is_tolerated(self, tmp_path):
        """容忍一次跳过（单次网络抖动），不制造噪音告警。"""
        (tmp_path / "300308-consensus.json").write_text(
            json.dumps({"fetched_at": "2026-08-03T08:45:00+08:00"}))

        assert rdh.check_consensus_cadence("300308", tmp_path, date(2026, 8, 11)) is None

    def test_two_missed_runs_alerts(self, tmp_path):
        """8-03 之后 8-10、8-17 两个周一都没更新 → 真异常。"""
        (tmp_path / "300308-consensus.json").write_text(
            json.dumps({"fetched_at": "2026-08-03T08:45:00+08:00"}))

        r = rdh.check_consensus_cadence("300308", tmp_path, date(2026, 8, 18))

        assert r is not None
        assert "应跑" in r["issue"] or "次" in r["issue"]

    def test_fresh_fetch_passes(self, tmp_path):
        (tmp_path / "300308-consensus.json").write_text(
            json.dumps({"fetched_at": "2026-08-10T08:45:00+08:00"}))

        assert rdh.check_consensus_cadence("300308", tmp_path, date(2026, 8, 11)) is None

    def test_missing_file_left_to_other_check(self, tmp_path):
        assert rdh.check_consensus_cadence("999999", tmp_path, date(2026, 8, 11)) is None


# ── ② revision momentum 支持营收，亏损标的自动退化 ───────────────────────────


class TestRevisionMomentumMetrics:
    def _hist(self, tmp_path, series, key="688498"):
        p = tmp_path / f"{key}-consensus-history.jsonl"
        p.write_text("\n".join(json.dumps({
            "as_of": d, "years": {"2027E": {"profit": pr, "revenue": rv, "count": 7}}
        }) for d, pr, rv in series) + "\n")
        return p

    def test_revenue_metric_is_computable(self, tmp_path):
        """★动机案例：源杰 2027E **营收**预期被下调 24%。
        补这个之前，实现算不出自己的论据。"""
        self._hist(tmp_path, [("2026-07-01", 1.5e9, 3.5e9), ("2026-08-07", 1.4e9, 2.66e9)])

        r = urc.revision_momentum("688498", "2027E", tmp_path, metric="revenue")

        assert r["direction"] == "down"
        assert r["change_pct"] == pytest.approx(-24.0, abs=0.5)

    def test_profit_remains_the_default(self, tmp_path):
        self._hist(tmp_path, [("2026-07-01", 1.0e9, 3.0e9), ("2026-08-07", 1.2e9, 3.0e9)])

        r = urc.revision_momentum("688498", "2027E", tmp_path)

        assert r["metric"] == "profit"
        assert r["change_pct"] == pytest.approx(20.0, abs=0.1)

    def test_loss_making_falls_back_to_revenue(self, tmp_path):
        """智谱：profit 为负（−44 亿）算不出百分比，但 revenue 是正的完全能算。
        自动退化并**标明用的是哪个口径**，否则读者会以为看的是利润修正。"""
        self._hist(tmp_path, [("2026-07-01", -4.4e9, 5.0e9), ("2026-08-07", -4.0e9, 6.0e9)],
                   key="02513")

        r = urc.revision_momentum("02513", "2027E", tmp_path)

        assert r["metric"] == "revenue"
        assert r["direction"] == "up"
        assert r["change_pct"] == pytest.approx(20.0, abs=0.1)

    def test_explicit_revenue_request_is_not_overridden(self, tmp_path):
        self._hist(tmp_path, [("2026-07-01", 1.0e9, 3.0e9), ("2026-08-07", 1.0e9, 3.6e9)])

        r = urc.revision_momentum("688498", "2027E", tmp_path, metric="revenue")

        assert r["metric"] == "revenue"

    def test_both_metrics_unusable_is_insufficient(self, tmp_path):
        self._hist(tmp_path, [("2026-07-01", -1.0e9, None), ("2026-08-07", -2.0e9, None)])

        assert urc.revision_momentum("688498", "2027E", tmp_path)["direction"] == "insufficient"


# ── ③ history 写入静默失败要能被发现 ─────────────────────────────────────────


class TestHistoryKeepsUpWithConsensus:
    def _pair(self, tmp_path, consensus_date, history_dates):
        (tmp_path / "300308-consensus.json").write_text(
            json.dumps({"fetched_at": f"{consensus_date}T08:45:00+08:00"}))
        if history_dates is not None:
            (tmp_path / "300308-consensus-history.jsonl").write_text(
                "\n".join(json.dumps({"as_of": d, "years": {}}) for d in history_dates) + "\n")

    def test_history_in_sync_passes(self, tmp_path):
        self._pair(tmp_path, "2026-08-10", ["2026-08-03", "2026-08-10"])

        assert rdh.check_history_keeps_up("300308", tmp_path) is None

    def test_history_behind_consensus_is_flagged(self, tmp_path):
        """★静默失败的形态：consensus 刷到 8-10 了，history 还停在 8-03。
        不报的话 momentum 永远 insufficient，而这和「刚开始积累」无法区分。"""
        self._pair(tmp_path, "2026-08-10", ["2026-08-03"])

        r = rdh.check_history_keeps_up("300308", tmp_path)

        assert r is not None
        assert "修正历史" in r["issue"]

    def test_missing_history_file_is_flagged(self, tmp_path):
        self._pair(tmp_path, "2026-08-10", None)

        assert rdh.check_history_keeps_up("300308", tmp_path) is not None

    def test_no_consensus_file_is_left_to_other_checks(self, tmp_path):
        assert rdh.check_history_keeps_up("999999", tmp_path) is None
