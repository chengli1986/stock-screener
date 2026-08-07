#!/usr/bin/env python3
"""test_consensus_history.py — 预期修正方向（revision momentum）的历史积累

## 为什么要单独存历史

`{key}-consensus.json` 是**覆盖写**的，每次 cron 跑完就没有上一版了。
想知道「机构在上调还是下调预期」，必须有跨时间的观测点。

一个自然的想法是从 git 历史刨——2026-08-07 实测否决了它：
`data/300308-consensus.json` 共 5 个版本，**全部落在 8-03～8-04 两天内**，
是开发迭代的产物而非时间序列；而且 commit 日期 ≠ 观测日期
（同一天可以提交三次，也可以隔几天才提交一次抓到的数据）。

所以改为**显式追加**一份 `{key}-consensus-history.jsonl`，一次抓取一行。

## 为什么修正方向值得单独做

它是这套管线里唯一可能带 alpha 的维度：估值倍数、财务、同业市值都是「现状」，
而预期被上调还是下调是**市场看法的变化率**。这轮回撤里的实证——
旭创跌 33% 而 2027E 预期被上调 14%（错位，值得看）；
源杰跌 35% 但 2027E 营收预期被下调 24%（基本面恶化，不该买）——
同样是深跌，方向完全相反，只有修正数据能区分。

## 口径

- 用 `broker_stats.preferred_value`（截尾均值，用户选定的分母口径），
  没有则退回 `estimates`，与页面/告警保持同一口径。
- **同一天多次抓取只保留最后一次**：开发期会重复跑，不能让它们冒充多个观测点。
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


def _payload(p26=1.41e11, p27=2.41e11, count=2, fetched="2026-08-07T08:45:00+08:00"):
    return {
        "symbol": "688825", "name": "长鑫科技", "fetched_at": fetched,
        "estimates": {"2026E": {"profit_yuan": p26, "revenue_yuan": 2.8e11},
                      "2027E": {"profit_yuan": p27, "revenue_yuan": 4.3e11}},
        "broker_stats": {"2026E": {"preferred_value": p26 * 1.05, "count": count},
                         "2027E": {"preferred_value": p27 * 1.03, "count": count}},
    }


# ── 抽取一条观测 ──────────────────────────────────────────────────────────────


class TestObservationExtraction:
    def test_records_observation_date_from_fetched_at(self):
        obs = urc.build_history_observation(_payload())

        assert obs["as_of"] == "2026-08-07"

    def test_uses_preferred_value_not_raw_estimate(self):
        """与页面、买点告警同口径（截尾均值），否则三处各说各话。"""
        obs = urc.build_history_observation(_payload(p26=1.0e11))

        assert obs["years"]["2026E"]["profit"] == pytest.approx(1.0e11 * 1.05)

    def test_falls_back_to_estimates_when_no_broker_stats(self):
        p = _payload()
        del p["broker_stats"]
        obs = urc.build_history_observation(p)

        assert obs["years"]["2026E"]["profit"] == pytest.approx(1.41e11)

    def test_keeps_broker_count_to_detect_coverage_change(self):
        """机构数变化本身是信号——新增覆盖机构常伴随预期跳变。"""
        obs = urc.build_history_observation(_payload(count=4))

        assert obs["years"]["2026E"]["count"] == 4


# ── 追加与同日去重 ────────────────────────────────────────────────────────────


class TestAppendHistory:
    def test_creates_file_on_first_write(self, tmp_path):
        urc.append_history("688825", _payload(), tmp_path)

        lines = (tmp_path / "688825-consensus-history.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1

    def test_appends_second_observation(self, tmp_path):
        urc.append_history("688825", _payload(fetched="2026-08-07T08:45:00+08:00"), tmp_path)
        urc.append_history("688825", _payload(fetched="2026-08-10T08:45:00+08:00"), tmp_path)

        lines = (tmp_path / "688825-consensus-history.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

    def test_same_day_rerun_replaces_instead_of_duplicating(self, tmp_path):
        """★开发期一天会跑很多次。若都留下，会冒充成多个观测点，
        把「一天内的重复抓取」误读成「预期在剧烈修正」。"""
        urc.append_history("688825", _payload(p26=1.0e11), tmp_path)
        urc.append_history("688825", _payload(p26=2.0e11), tmp_path)

        lines = (tmp_path / "688825-consensus-history.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["years"]["2026E"]["profit"] == pytest.approx(2.0e11 * 1.05)

    def test_history_is_ordered_by_date(self, tmp_path):
        urc.append_history("688825", _payload(fetched="2026-08-10T08:45:00+08:00"), tmp_path)
        urc.append_history("688825", _payload(fetched="2026-08-07T08:45:00+08:00"), tmp_path)

        lines = (tmp_path / "688825-consensus-history.jsonl").read_text().strip().split("\n")
        dates = [json.loads(x)["as_of"] for x in lines]
        assert dates == sorted(dates)


# ── 修正方向 ──────────────────────────────────────────────────────────────────


class TestRevisionMomentum:
    def _hist(self, tmp_path, series):
        p = tmp_path / "300308-consensus-history.jsonl"
        p.write_text("\n".join(json.dumps({
            "as_of": d, "years": {"2027E": {"profit": v, "count": 30}}}) for d, v in series) + "\n")
        return p

    def test_upward_revision_is_positive(self, tmp_path):
        """旭创实证：跌 33% 而 2027E 预期被上调 14% —— 错位，值得看。"""
        self._hist(tmp_path, [("2026-07-01", 4.8e10), ("2026-08-07", 5.472e10)])

        r = urc.revision_momentum("300308", "2027E", tmp_path)

        assert r["change_pct"] == pytest.approx(14.0, abs=0.1)
        assert r["direction"] == "up"

    def test_downward_revision_is_negative(self, tmp_path):
        """源杰实证：跌 35% 但 2027E 营收预期被下调 24% —— 基本面恶化。"""
        self._hist(tmp_path, [("2026-07-01", 1.0e10), ("2026-08-07", 0.76e10)])

        r = urc.revision_momentum("300308", "2027E", tmp_path)

        assert r["direction"] == "down"
        assert r["change_pct"] == pytest.approx(-24.0, abs=0.1)

    def test_flat_within_noise_band_is_flat(self, tmp_path):
        """±2% 内不算修正 —— 机构小幅调整模型参数是常态，不是观点变化。"""
        self._hist(tmp_path, [("2026-07-01", 1.0e10), ("2026-08-07", 1.01e10)])

        assert urc.revision_momentum("300308", "2027E", tmp_path)["direction"] == "flat"

    def test_single_observation_yields_insufficient_not_zero(self, tmp_path):
        """★只有一个点时必须说「数据不足」，不能报 0% ——
        0% 会被读成「预期稳定」，而事实是我们根本不知道。"""
        self._hist(tmp_path, [("2026-08-07", 4.8e10)])

        r = urc.revision_momentum("300308", "2027E", tmp_path)

        assert r["direction"] == "insufficient"
        assert r["change_pct"] is None

    def test_missing_file_is_insufficient(self, tmp_path):
        r = urc.revision_momentum("999999", "2027E", tmp_path)

        assert r["direction"] == "insufficient"

    def test_reports_span_so_reader_can_judge_significance(self, tmp_path):
        """跨 37 天的 14% 与跨 1 天的 14% 意义完全不同。"""
        self._hist(tmp_path, [("2026-07-01", 4.8e10), ("2026-08-07", 5.472e10)])

        r = urc.revision_momentum("300308", "2027E", tmp_path)

        assert r["span_days"] == 37
        assert r["from_date"] == "2026-07-01"
        assert r["to_date"] == "2026-08-07"

    def test_uses_earliest_within_lookback_window(self, tmp_path):
        """比较基准取窗口内最早的一点，而不是上一次 —— 周频抓取时
        「与上次比」几乎总是 flat，看不出趋势。"""
        self._hist(tmp_path, [("2026-06-01", 4.0e10), ("2026-07-01", 4.5e10),
                              ("2026-08-07", 5.0e10)])

        r = urc.revision_momentum("300308", "2027E", tmp_path, lookback_days=90)

        assert r["from_date"] == "2026-06-01"
        assert r["change_pct"] == pytest.approx(25.0, abs=0.1)

    def test_ignores_observations_outside_lookback(self, tmp_path):
        self._hist(tmp_path, [("2026-01-01", 1.0e10), ("2026-07-01", 4.5e10),
                              ("2026-08-07", 5.0e10)])

        r = urc.revision_momentum("300308", "2027E", tmp_path, lookback_days=90)

        assert r["from_date"] == "2026-07-01"


# ── 接线：算了没接 = 白算 ─────────────────────────────────────────────────────


class TestWiredIntoWriteAndDeploy:
    """这套管线已经犯过一次「算了没接」的错——`preferred_stat` 落盘了，
    页面和叙事却仍在用旧口径。故把 append_history 放进 write_and_deploy 内部
    （而非 main 的两处调用点旁边），并在此钉死接线。"""

    def test_write_and_deploy_also_appends_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr(urc, "DOCS_DATA", tmp_path)
        monkeypatch.setattr(urc, "DEPLOY_DATA", tmp_path / "nonexistent")

        urc.write_and_deploy("688825", _payload())

        assert (tmp_path / "688825-consensus.json").exists()
        assert (tmp_path / "688825-consensus-history.jsonl").exists()

    def test_history_failure_does_not_break_current_data(self, tmp_path, monkeypatch):
        """历史是加分项，写失败不该让当期 consensus 也落不下去。"""
        monkeypatch.setattr(urc, "DOCS_DATA", tmp_path)
        monkeypatch.setattr(urc, "DEPLOY_DATA", tmp_path / "nonexistent")
        monkeypatch.setattr(urc, "append_history",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

        urc.write_and_deploy("688825", _payload())

        assert (tmp_path / "688825-consensus.json").exists()
