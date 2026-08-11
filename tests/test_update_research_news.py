#!/usr/bin/env python3
"""test_update_research_news.py — 采集合成与落盘

★不测网络。`fetch_*` 只在集成时手动验证；本文件测的是**合成逻辑与失败模式**，
那才是会静默出错的地方。
"""

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "update_research_news", _ROOT / "scripts" / "update_research_news.py")
urn = importlib.util.module_from_spec(_spec)
sys.modules["update_research_news"] = urn
_spec.loader.exec_module(urn)

NOW = "2026-08-11"


def ann(title, date, category=None):
    return {"kind": "announcement", "title": title, "date": date,
            "url": "https://cninfo/" + date + title[:6], "category": category}


def news(title, date):
    return {"kind": "news", "title": title, "date": date,
            "url": "https://em/" + date + title[:6], "source": "东方财富"}


# ── 分层排序 ─────────────────────────────────────────────────────────────────


class TestLayerOrdering:
    _ITEMS_A = [ann("2026年半年度业绩预告", "2026-07-14"),
                ann("关于完成董事会换届选举及聘任高级管理人员的公告", "2026-07-20"),
                ann("北京市金杜律师事务所之法律意见书", "2026-08-07")]
    _ITEMS_N = [news("中际旭创300308龙虎榜数据07-28", "2026-07-28"),
                news("Bernstein 首次覆盖", "2026-08-04")]

    def test_layers_appear_in_fixed_order(self):
        p = urn.build_payload("300308", "中际旭创", self._ITEMS_A, self._ITEMS_N, NOW)

        assert [g["layer"] for g in p["groups"]] == [
            "major", "substantive", "news", "procedural", "technical"]

    def test_major_is_first(self):
        p = urn.build_payload("300308", "中际旭创", self._ITEMS_A, self._ITEMS_N, NOW)

        assert p["groups"][0]["items"][0]["title"] == "2026年半年度业绩预告"

    def test_technical_is_last(self):
        p = urn.build_payload("300308", "中际旭创", self._ITEMS_A, self._ITEMS_N, NOW)

        assert "龙虎榜" in p["groups"][-1]["items"][0]["title"]

    def test_empty_layers_are_omitted(self):
        """没有大事时不该留一个空的「重要事项」标题。"""
        p = urn.build_payload("300308", "中际旭创",
                              [ann("关于董事会换届的公告", "2026-07-20")], [], NOW)

        assert [g["layer"] for g in p["groups"]] == ["substantive"]

    def test_within_layer_newest_first(self):
        items = [ann("关于董事会换届的公告", "2026-07-01"),
                 ann("关于聘任财务总监的公告", "2026-08-01")]
        p = urn.build_payload("300308", "中际旭创", items, [], NOW)

        dates = [x["date"] for x in p["groups"][0]["items"]]
        assert dates == sorted(dates, reverse=True)


# ── 30 天窗口 ────────────────────────────────────────────────────────────────


class TestWindow:
    def test_items_older_than_30_days_dropped(self):
        p = urn.build_payload("300308", "中际旭创",
                              [ann("关于董事会换届的公告", "2026-06-01")], [], NOW)

        assert p["groups"] == []

    def test_item_exactly_30_days_old_kept(self):
        p = urn.build_payload("300308", "中际旭创",
                              [ann("关于董事会换届的公告", "2026-07-12")], [], NOW)

        assert len(p["groups"]) == 1


# ── 空态：不留白 ─────────────────────────────────────────────────────────────


class TestEmptyState:
    def test_no_announcements_is_stated_not_blank(self):
        """★实测长光华芯近 30 天 0 条公告。今天的教训：宁可说明原因，也不要留白。"""
        p = urn.build_payload("688048", "长光华芯", [], [news("某新闻", "2026-08-05")], NOW)

        assert p["announcements_empty"] is True

    def test_has_announcements_flag_false_when_present(self):
        p = urn.build_payload("300308", "中际旭创",
                              [ann("关于董事会换届的公告", "2026-08-01")], [], NOW)

        assert p["announcements_empty"] is False

    def test_everything_empty_marks_payload_empty(self):
        p = urn.build_payload("688048", "长光华芯", [], [], NOW)

        assert p["is_empty"] is True


# ── 新闻攒历史 ───────────────────────────────────────────────────────────────


class TestNewsHistory:
    def test_first_run_writes_all(self, tmp_path):
        got = urn.merge_news_history("300308", [news("A", "2026-08-01")], tmp_path)

        assert len(got) == 1
        assert (tmp_path / "300308-news-raw.jsonl").exists()

    def test_rerun_does_not_duplicate(self, tmp_path):
        """★东财每天返回同一批最新 10 条，不去重会迅速堆成垃圾。"""
        urn.merge_news_history("300308", [news("A", "2026-08-01")], tmp_path)
        got = urn.merge_news_history("300308", [news("A", "2026-08-01")], tmp_path)

        assert len(got) == 1

    def test_accumulates_across_runs(self, tmp_path):
        """★这是攒历史的全部目的：接口固定 10 条，盛科 10 条只覆盖 4 天，
        直接读拼不出一个月。"""
        urn.merge_news_history("300308", [news("A", "2026-08-01")], tmp_path)
        got = urn.merge_news_history("300308", [news("B", "2026-08-02")], tmp_path)

        assert len(got) == 2

    def test_returns_only_recent_window(self, tmp_path):
        urn.merge_news_history("300308", [news("旧闻", "2026-05-01")], tmp_path)
        got = urn.merge_news_history("300308", [news("新闻", "2026-08-10")], tmp_path,
                                     now=NOW, days=30)

        assert [x["title"] for x in got] == ["新闻"]

    def test_old_rows_stay_on_disk(self, tmp_path):
        """窗口只影响返回值，不该删磁盘上的历史。"""
        urn.merge_news_history("300308", [news("旧闻", "2026-05-01")], tmp_path)
        urn.merge_news_history("300308", [news("新闻", "2026-08-10")], tmp_path, now=NOW)

        rows = (tmp_path / "300308-news-raw.jsonl").read_text().strip().splitlines()
        assert len(rows) == 2


# ── 失败不覆盖 ───────────────────────────────────────────────────────────────


class TestFailureDoesNotClobber:
    def test_empty_payload_does_not_overwrite_existing_file(self, tmp_path):
        """★抓取失败时若照常写盘，会把昨天好的数据换成一个空壳。"""
        target = tmp_path / "300308-news.json"
        target.write_text(json.dumps({"groups": [{"layer": "major", "items": [1]}]}),
                          encoding="utf-8")

        urn.write_and_deploy("300308", {"is_empty": True, "groups": []}, tmp_path, None)

        assert json.loads(target.read_text())["groups"][0]["layer"] == "major"

    def test_non_empty_payload_does_overwrite(self, tmp_path):
        target = tmp_path / "300308-news.json"
        target.write_text(json.dumps({"groups": []}), encoding="utf-8")
        payload = {"is_empty": False,
                   "groups": [{"layer": "major", "items": [{"title": "新的"}]}]}

        urn.write_and_deploy("300308", payload, tmp_path, None)

        assert json.loads(target.read_text())["groups"][0]["items"][0]["title"] == "新的"

    def test_writes_when_no_existing_file(self, tmp_path):
        urn.write_and_deploy("300308", {"is_empty": True, "groups": []}, tmp_path, None)

        assert (tmp_path / "300308-news.json").exists()
