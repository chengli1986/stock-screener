#!/usr/bin/env python3
"""test_divergence_alert_transitions.py — 背离告警改为「状态变化时才响」

## 2026-08-09 验证告警通道时发现的三个问题

**① 页面和告警对「问题」的定义不一致。**
长鑫 2027E 两源区间只重叠 7.3%，页面把它当问题显示（「样本分歧」），
但告警不认——因为 `cross_check` 判的是 CONFIRMED，而 `range_agreement`
的 `SAMPLE_DIVERGENT` 根本不在告警条件里。同一套数据，页面说有问题、告警说没事。

**② 直接把 SAMPLE_DIVERGENT 加进告警会制造噪音。**
长鑫的样本分歧是**持续状态**（它就是只有 4 家机构覆盖），改成全年每周一抓之后，
每周一都会收到同一封邮件——两周后这个告警就会被无视，等于自我废弃。

**③ 告警只说「请留意」，没说该怎么办**，也没有链接回研报页。

## 解法：比对上一次观测的状态，只在**变化**时告警

- `CONFIRMED → DIVERGENT`：恶化，告警
- `DIVERGENT → CONFIRMED`：恢复，也告警（同样是有用的信息）
- `DIVERGENT → DIVERGENT`：持续状态，**不告警**
- 首次出现（无历史）：告警一次——你不知道它之前是什么样

状态存进 `{key}-consensus-history.jsonl`（该文件 8-07 已建，本次加 verdict 字段）。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_consensus.py"
_spec = importlib.util.spec_from_file_location("update_research_consensus", _SCRIPT)
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)


def _record(cross="CONFIRMED", rng="ALIGNED", year="2027E"):
    return {
        "symbol": "688256", "name": "寒武纪", "fetched_at": "2026-08-09T08:45:00+08:00",
        "estimates": {year: {"profit_yuan": 9.5e9, "revenue_yuan": 2.6e10}},
        "broker_stats": {year: {"preferred_value": 9.5e9, "count": 10}},
        "cross_check": {year: {"profit": {"verdict": cross, "diff_pct": -11.5,
                                          "primary": 9.5e9, "secondary": 1.07e10}}},
        "range_agreement": {year: {"verdict": rng, "overlap_pct": 42.4}},
    }


def _seed_history(tmp_path, key, states):
    """states: [(date, cross_verdict, range_verdict), ...]"""
    p = tmp_path / f"{key}-consensus-history.jsonl"
    p.write_text("\n".join(json.dumps({
        "as_of": d, "years": {"2027E": {"profit": 9.5e9, "count": 10,
                                        "verdict": cv, "range_verdict": rv}}
    }) for d, cv, rv in states) + "\n", encoding="utf-8")
    return p


# ── 观测里要带上状态 ─────────────────────────────────────────────────────────


class TestObservationCarriesVerdict:
    def test_cross_check_verdict_is_recorded(self):
        obs = urc.build_history_observation(_record(cross="DIVERGENT"))

        assert obs["years"]["2027E"]["verdict"] == "DIVERGENT"

    def test_range_verdict_is_recorded(self):
        obs = urc.build_history_observation(_record(rng="SAMPLE_DIVERGENT"))

        assert obs["years"]["2027E"]["range_verdict"] == "SAMPLE_DIVERGENT"

    def test_absent_verdict_is_none_not_confirmed(self):
        """港股无跨源——不能把「没有」记成「已确认」。"""
        rec = _record()
        del rec["cross_check"]
        obs = urc.build_history_observation(rec)

        assert obs["years"]["2027E"]["verdict"] is None


# ── 只在状态变化时告警 ───────────────────────────────────────────────────────


class TestTransitionDetection:
    def test_newly_divergent_is_reported(self, tmp_path):
        _seed_history(tmp_path, "688256", [("2026-08-03", "CONFIRMED", "ALIGNED")])

        tr = urc.verdict_transitions("688256", _record(cross="DIVERGENT"), tmp_path)

        assert len(tr) == 1
        assert tr[0]["kind"] == "worsened"
        assert tr[0]["year"] == "2027E"

    def test_recovery_is_also_reported(self, tmp_path):
        """恢复同样是有用信息——之前警告过的标的现在两源一致了。"""
        _seed_history(tmp_path, "688256", [("2026-08-03", "DIVERGENT", "ALIGNED")])

        tr = urc.verdict_transitions("688256", _record(cross="CONFIRMED"), tmp_path)

        assert len(tr) == 1
        assert tr[0]["kind"] == "recovered"

    def test_persistent_divergence_is_silent(self, tmp_path):
        """★核心：长鑫的样本分歧是持续状态，每周告警会让人两周后就无视它。"""
        _seed_history(tmp_path, "688256", [("2026-08-03", "DIVERGENT", "ALIGNED")])

        assert urc.verdict_transitions("688256", _record(cross="DIVERGENT"), tmp_path) == []

    def test_persistent_healthy_is_silent(self, tmp_path):
        _seed_history(tmp_path, "688256", [("2026-08-03", "CONFIRMED", "ALIGNED")])

        assert urc.verdict_transitions("688256", _record(), tmp_path) == []

    def test_first_observation_reports_problems(self, tmp_path):
        """无历史时不知道之前什么样，有问题就报一次；之后转为持续状态便不再打扰。"""
        tr = urc.verdict_transitions("688256", _record(cross="DIVERGENT"), tmp_path)

        assert len(tr) == 1
        assert tr[0]["kind"] == "first_seen"

    def test_first_observation_healthy_is_silent(self, tmp_path):
        assert urc.verdict_transitions("688256", _record(), tmp_path) == []

    def test_range_divergence_now_counts(self, tmp_path):
        """★页面认为样本分歧是问题，告警此前不认——本次对齐。"""
        _seed_history(tmp_path, "688256", [("2026-08-03", "CONFIRMED", "ALIGNED")])

        tr = urc.verdict_transitions("688256", _record(rng="SAMPLE_DIVERGENT"), tmp_path)

        assert len(tr) == 1
        assert "样本分歧" in tr[0]["what"]

    def test_uses_latest_history_entry_not_first(self, tmp_path):
        _seed_history(tmp_path, "688256", [
            ("2026-07-06", "DIVERGENT", "ALIGNED"),
            ("2026-08-03", "CONFIRMED", "ALIGNED")])

        tr = urc.verdict_transitions("688256", _record(cross="DIVERGENT"), tmp_path)

        assert len(tr) == 1 and tr[0]["kind"] == "worsened"

    def test_same_day_rerun_uses_the_latest_entry(self, tmp_path):
        """★这条初版写反了，实跑才发现。

        初版断言「当天重跑要跳过同日、跟前一天比」，据此实现后第二次跑仍然
        每次都报「新出现」并重复发邮件——因为跳过今天那条后，退回到的是更早的
        旧格式记录（还没有 verdict 字段），于是永远判成「从无问题变有问题」。

        正确语义：本函数在 write_and_deploy **之前**执行，文件里最后一条必然是
        「上一次的状态」。同日条目正是上一次跑写的，恰恰应该拿来比。
        """
        _seed_history(tmp_path, "688256", [
            ("2026-08-03", "CONFIRMED", "ALIGNED"),
            ("2026-08-09", "DIVERGENT", "ALIGNED")])

        # 上次已是 DIVERGENT，本次仍是 → 持续状态，静默
        assert urc.verdict_transitions("688256", _record(cross="DIVERGENT"), tmp_path) == []

    def test_same_day_rerun_still_detects_real_change(self, tmp_path):
        """同日重跑若状态真的变了，仍要报。"""
        _seed_history(tmp_path, "688256", [
            ("2026-08-03", "CONFIRMED", "ALIGNED"),
            ("2026-08-09", "CONFIRMED", "ALIGNED")])

        tr = urc.verdict_transitions("688256", _record(cross="DIVERGENT"), tmp_path)

        assert len(tr) == 1 and tr[0]["kind"] == "worsened"


# ── 告警正文要说清「该怎么办」 ───────────────────────────────────────────────


class TestAlertBody:
    _TR = [{"name": "寒武纪", "year": "2027E", "kind": "worsened",
            "what": "两源背离", "detail": "同花顺 261.3 亿 vs 东财 291.3 亿（−10.3%）",
            "page": "/cambricon-688256.html"}]

    def test_states_what_to_do(self):
        """原文只说「请留意」，读者不知道下一步做什么。"""
        html = urc.build_transition_alert_html(self._TR, "2026-08-09T08:45:00+08:00")

        assert "研报页" in html or "核对" in html

    def test_links_back_to_report_page(self):
        html = urc.build_transition_alert_html(self._TR, "2026-08-09T08:45:00+08:00")

        assert "/cambricon-688256.html" in html

    def test_separates_worsened_from_recovered(self):
        tr = self._TR + [{"name": "三环集团", "year": "2026E", "kind": "recovered",
                          "what": "两源背离", "detail": "已恢复一致", "page": "/sanhuan-300408.html"}]
        html = urc.build_transition_alert_html(tr, "2026-08-09T08:45:00+08:00")

        assert "恢复" in html

    def test_empty_yields_none(self):
        assert urc.build_transition_alert_html([], "2026-08-09T08:45:00+08:00") is None


class TestHorizonGrouping:
    """用户 2026-08-05 定「2028E 太远」。视界外的背离仍是数据质量信号，
    但混在一起会让读者第一反应「2028 我不看」而连视界内的一起忽略。"""

    def test_beyond_horizon_is_separated(self):
        tr = [{"name": "寒武纪", "year": "2028E", "kind": "worsened", "what": "两源背离",
               "detail": "同花顺 183.4亿 vs 东财 162.4亿（-11.5%）", "page": "/cambricon-688256.html"}]
        html = urc.build_transition_alert_html(tr, "2026-08-09T08:45:00+08:00")

        assert "视界外" in html

    def test_in_horizon_not_marked_as_beyond(self):
        tr = [{"name": "长鑫科技", "year": "2027E", "kind": "worsened", "what": "两源样本分歧",
               "detail": "仅重叠 7.3%", "page": "/cxmt-ipo.html"}]
        html = urc.build_transition_alert_html(tr, "2026-08-09T08:45:00+08:00")

        assert "视界外" not in html
