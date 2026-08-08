#!/usr/bin/env python3
"""test_data_health_consensus.py — 估值分母的两个监控盲区

## 盲区一：兜底静默生效（本次主因）

`resolve_consensus()` 的设计是「自动抓取值优先，拿不到就回落 `research_stocks.json`
里的注册表值」。但注册表里那些数字是**登记那天写下的、此后从未更新**
（旭创写 480 亿，实际 548 亿，差 14%）——这正是 2026-08-03 查出的「化石分母」。

所以某次抓取失败时，页面不会空、不会报错，而是**平静地显示一个错 14% 的 PE**。
`consensus_source: "registry"` 字段确实记下了，但没有任何人会去看它。

在「读研报下投资判断」这个用法下，**静默的错数字比明显的空值危险得多**。

## 盲区二：consensus.json 自己没有陈旧检查

`research_data_health.py` 原本只查 snapshot / financials / peers-market 三类。
偏偏漏掉了 consensus —— 而它是估值分母的来源，月更（每月 1 日）。
若某月 cron 整个失败，snapshot 仍会每天刷新（分子是新的），分母却停在上个月，
三类检查全部显示「新鲜」。

## 为什么阈值取 40 天

与 financials 同为月更，同阈值。8-31 半年报季后预期会集中调整，
届时另行加密频率（本次不改频率，只补监控）。
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research_data_health.py"
_spec = importlib.util.spec_from_file_location("research_data_health", _SCRIPT)
rdh = importlib.util.module_from_spec(_spec)
sys.modules["research_data_health"] = rdh
_spec.loader.exec_module(rdh)

TODAY = date(2026, 8, 7)


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ── 盲区一：兜底静默生效 ──────────────────────────────────────────────────────


class TestFallbackToRegistryIsReported:
    def test_registry_source_is_flagged(self, tmp_path):
        _write(tmp_path, "300308-snapshot.json",
               {"symbol": "300308", "name": "中际旭创", "as_of": "2026-08-07",
                "consensus_source": "registry"})

        found = rdh.check_consensus_source(
            [{"snapshot_key": "300308", "name": "中际旭创"}], tmp_path, TODAY)

        assert len(found) == 1
        assert "300308" in found[0]["file"]

    def test_auto_source_is_not_flagged(self, tmp_path):
        _write(tmp_path, "300308-snapshot.json",
               {"symbol": "300308", "as_of": "2026-08-07", "consensus_source": "auto"})

        assert rdh.check_consensus_source(
            [{"snapshot_key": "300308", "name": "中际旭创"}], tmp_path, TODAY) == []

    def test_issue_text_states_the_consequence_not_just_the_state(self, tmp_path):
        """告警要说「页面正在显示可能过时的估值倍数」，而不是只说
        「consensus_source=registry」——后者读的人无从判断严重性。"""
        _write(tmp_path, "600519-snapshot.json",
               {"symbol": "600519", "as_of": "2026-08-07", "consensus_source": "registry"})

        found = rdh.check_consensus_source(
            [{"snapshot_key": "600519", "name": "贵州茅台"}], tmp_path, TODAY)

        assert "兜底" in found[0]["issue"] or "注册表" in found[0]["issue"]
        assert "估值" in found[0]["issue"]

    def test_missing_field_is_not_treated_as_healthy(self, tmp_path):
        """老快照没有这个字段。缺失≠正常，应报出来而不是默认放行。"""
        _write(tmp_path, "300308-snapshot.json", {"symbol": "300308", "as_of": "2026-08-07"})

        found = rdh.check_consensus_source(
            [{"snapshot_key": "300308", "name": "中际旭创"}], tmp_path, TODAY)

        assert len(found) == 1

    def test_absent_snapshot_file_is_left_to_the_staleness_check(self, tmp_path):
        """文件根本不存在时已由 check_file 报「文件缺失」，这里不重复报。"""
        assert rdh.check_consensus_source(
            [{"snapshot_key": "999999", "name": "不存在"}], tmp_path, TODAY) == []


# ── 盲区二：consensus.json 陈旧 ───────────────────────────────────────────────


class TestConsensusStaleness:
    def test_fresh_consensus_passes(self, tmp_path):
        _write(tmp_path, "300308-consensus.json", {"fetched_at": "2026-08-01T08:45:00+08:00"})

        assert rdh.check_file(tmp_path / "300308-consensus.json", "fetched_at",
                              rdh.CONSENSUS_MAX_DAYS, TODAY) is None

    def test_two_month_old_consensus_is_stale(self, tmp_path):
        """★整月 cron 失败的形态：snapshot 天天刷新（分子新），分母停在两个月前。
        补这条之前，三类检查全部显示「新鲜」。"""
        _write(tmp_path, "300308-consensus.json", {"fetched_at": "2026-06-01T08:45:00+08:00"})

        r = rdh.check_file(tmp_path / "300308-consensus.json", "fetched_at",
                           rdh.CONSENSUS_MAX_DAYS, TODAY)

        assert r is not None
        assert "fetched_at" in r["issue"]

    def test_threshold_tolerates_a_normal_monthly_cycle(self, tmp_path):
        """每月 1 日刷新，月末检查时已 30 天出头 —— 不能误报。"""
        _write(tmp_path, "300308-consensus.json", {"fetched_at": "2026-07-01T08:45:00+08:00"})

        assert rdh.check_file(tmp_path / "300308-consensus.json", "fetched_at",
                              rdh.CONSENSUS_MAX_DAYS, TODAY) is None


# ── 汇总入口要真的把两项接进去（算出来没接 = 白算） ──────────────────────────


class TestCollectWiresBothChecks:
    def _stocks(self, tmp_path, monkeypatch, stocks):
        f = tmp_path / "research_stocks.json"
        f.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(rdh, "STOCKS_FILE", f)
        monkeypatch.setattr(rdh, "DOCS_DATA_DIR", tmp_path)
        monkeypatch.setattr(rdh, "PEERS_FILE", tmp_path / "no_peers.json")

    def test_collect_includes_registry_fallback(self, tmp_path, monkeypatch):
        self._stocks(tmp_path, monkeypatch, [{"snapshot_key": "300308", "name": "中际旭创"}])
        _write(tmp_path, "300308-snapshot.json",
               {"symbol": "300308", "as_of": "2026-08-07", "consensus_source": "registry"})
        _write(tmp_path, "300308-financials.json", {"updated_at": "2026-08-01"})
        _write(tmp_path, "300308-consensus.json", {"fetched_at": "2026-08-01T08:45:00+08:00"})

        issues = rdh.collect_stale(TODAY)

        assert any("兜底" in i["issue"] or "注册表" in i["issue"] for i in issues)

    def test_collect_includes_stale_consensus(self, tmp_path, monkeypatch):
        self._stocks(tmp_path, monkeypatch, [{"snapshot_key": "300308", "name": "中际旭创"}])
        _write(tmp_path, "300308-snapshot.json",
               {"symbol": "300308", "as_of": "2026-08-07", "consensus_source": "auto"})
        _write(tmp_path, "300308-financials.json", {"updated_at": "2026-08-01"})
        _write(tmp_path, "300308-consensus.json", {"fetched_at": "2026-05-01T08:45:00+08:00"})

        issues = rdh.collect_stale(TODAY)

        assert any("consensus" in i["file"] for i in issues)

    def test_all_healthy_yields_nothing(self, tmp_path, monkeypatch):
        self._stocks(tmp_path, monkeypatch, [{"snapshot_key": "300308", "name": "中际旭创"}])
        _write(tmp_path, "300308-snapshot.json",
               {"symbol": "300308", "as_of": "2026-08-07", "consensus_source": "auto"})
        _write(tmp_path, "300308-financials.json", {"updated_at": "2026-08-01"})
        # 8-03 是加密月的周一，取它才不会触发 check_consensus_cadence（8-01 起算会漏 2 次）
        _write(tmp_path, "300308-consensus.json", {"fetched_at": "2026-08-03T08:45:00+08:00"})
        # 2026-08-08 新增 check_history_keeps_up 后，「健康」的定义也包含历史跟得上
        (tmp_path / "300308-consensus-history.jsonl").write_text(
            json.dumps({"as_of": "2026-08-03", "years": {}}) + "\n", encoding="utf-8")

        assert rdh.collect_stale(TODAY) == []
