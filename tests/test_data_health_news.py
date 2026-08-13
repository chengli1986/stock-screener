#!/usr/bin/env python3
"""test_data_health_news.py — research_data_health.py 补 news 监控盲区

## 洞是什么

`research_data_health.py` 原本监控 snapshot / financials / peers-market / consensus
四类，唯独没有 news——`update_research_news.py` 的 cron 若被误删/被并发编辑覆盖，
根本不会跑，页面 `{key}-news.json` 的 `as_of` 会一天天变旧而无人知晓：cron-wrapper
只在脚本**跑了但失败**时告警，「根本没跑」没有任何失败可报。

## 两类检查，成因不同，分开报（否则以后自己都看不懂）

- (a) `as_of` 陈旧（`NEWS_MAX_DAYS=3`）——见 `check_file` 复用同一套逻辑。
- (b) `announcements_error: true`（`check_news_announcements_error`）——本次公告
  抓取失败、公告层未知。

`write_and_deploy()`（update_research_news.py）在 `announcements_error` 为真时会
拒绝覆盖磁盘上的旧文件（刻意保护），`as_of` 因此停住不动——所以同一只股票可能
**同时**触发 (a) 和 (b)，这是预期行为，两条测试都覆盖了这个组合。
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research_data_health.py"
_spec = importlib.util.spec_from_file_location("research_data_health", _SCRIPT)
rdh = importlib.util.module_from_spec(_spec)
sys.modules["research_data_health"] = rdh
_spec.loader.exec_module(rdh)

TODAY = date(2026, 8, 13)


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ── (a) as_of 陈旧 ────────────────────────────────────────────────────────────


class TestNewsStaleness:
    def test_as_of_within_threshold_passes(self, tmp_path):
        _write(tmp_path, "300308-news.json",
               {"as_of": "2026-08-11", "announcements_error": False})  # 2 天前，阈值 3

        assert rdh.check_file(tmp_path / "300308-news.json", "as_of",
                              rdh.NEWS_MAX_DAYS, TODAY) is None

    def test_as_of_past_threshold_is_stale(self, tmp_path):
        _write(tmp_path, "300308-news.json",
               {"as_of": "2026-08-08", "announcements_error": False})  # 5 天前，超阈值 3

        r = rdh.check_file(tmp_path / "300308-news.json", "as_of", rdh.NEWS_MAX_DAYS, TODAY)

        assert r is not None
        assert "as_of" in r["issue"]

    def test_missing_file_is_stale(self, tmp_path):
        r = rdh.check_file(tmp_path / "300308-news.json", "as_of", rdh.NEWS_MAX_DAYS, TODAY)

        assert r is not None
        assert r["issue"] == "文件缺失"


# ── (b) announcements_error ──────────────────────────────────────────────────


class TestNewsAnnouncementsError:
    def test_error_true_is_flagged(self, tmp_path):
        _write(tmp_path, "300308-news.json",
               {"as_of": "2026-08-13", "announcements_error": True})

        found = rdh.check_news_announcements_error(
            [{"snapshot_key": "300308", "name": "中际旭创"}], tmp_path)

        assert len(found) == 1
        assert "300308" in found[0]["file"]

    def test_error_false_is_not_flagged(self, tmp_path):
        _write(tmp_path, "300308-news.json",
               {"as_of": "2026-08-13", "announcements_error": False})

        assert rdh.check_news_announcements_error(
            [{"snapshot_key": "300308", "name": "中际旭创"}], tmp_path) == []

    def test_missing_file_is_not_double_reported(self, tmp_path):
        """文件根本不存在时已由 check_file 报「文件缺失」，这里不重复报。"""
        assert rdh.check_news_announcements_error(
            [{"snapshot_key": "999999", "name": "不存在"}], tmp_path) == []

    def test_issue_text_is_worded_differently_from_staleness(self, tmp_path):
        """★两类成因不同，告警文案必须分开说清楚——这条直接锁住不能把
        announcements_error 的文案写成跟 as_of 陈旧一样的泛泛「已 N 天」。"""
        _write(tmp_path, "300308-news.json",
               {"as_of": "2026-08-13", "announcements_error": True})

        found = rdh.check_news_announcements_error(
            [{"snapshot_key": "300308", "name": "中际旭创"}], tmp_path)

        assert "announcements_error" in found[0]["issue"]
        assert "抓取失败" in found[0]["issue"]
        # 不能正面断言「无公告」——那是 announcements_empty 才能说的话（数据端的
        # 界限，见 update_research_news.py build_payload 的同一条区分）。文案里
        # 允许出现「不是『无公告』」这种澄清性否定，但不能出现「无公告披露」
        # 这句 announcements_empty 分支专用的正面措辞。
        assert "无公告披露" not in found[0]["issue"]


# ── 组合：collect_stale 里两类检查都要真的接进去 ──────────────────────────────


class TestCollectWiresNewsChecks:
    def _stocks(self, tmp_path, monkeypatch, stocks):
        f = tmp_path / "research_stocks.json"
        f.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(rdh, "STOCKS_FILE", f)
        monkeypatch.setattr(rdh, "DOCS_DATA_DIR", tmp_path)
        monkeypatch.setattr(rdh, "PEERS_FILE", tmp_path / "no_peers.json")

    def _healthy_non_news_files(self, tmp_path):
        """snapshot/financials/consensus/history 全部健康，只让 news 的状态
        决定 collect_stale() 的输出——隔离变量，不然分不清是哪个检查触发的。"""
        _write(tmp_path, "300308-snapshot.json",
               {"symbol": "300308", "as_of": "2026-08-13", "consensus_source": "auto"})
        _write(tmp_path, "300308-financials.json", {"updated_at": "2026-08-01"})
        _write(tmp_path, "300308-consensus.json", {"fetched_at": "2026-08-03T08:45:00+08:00"})
        (tmp_path / "300308-consensus-history.jsonl").write_text(
            json.dumps({"as_of": "2026-08-03", "years": {}}) + "\n", encoding="utf-8")

    def test_stale_as_of_only_is_reported(self, tmp_path, monkeypatch):
        """只跑不动：as_of 陈旧、announcements_error 却是 false ——采集大概率
        根本没跑（cron 没执行 / 整个脚本挂了）。"""
        self._stocks(tmp_path, monkeypatch, [{"snapshot_key": "300308", "name": "中际旭创"}])
        self._healthy_non_news_files(tmp_path)
        _write(tmp_path, "300308-news.json",
               {"as_of": "2026-08-08", "announcements_error": False})  # 5 天前

        issues = rdh.collect_stale(TODAY)

        news_issues = [i for i in issues if i["file"] == "300308-news.json"]
        assert len(news_issues) == 1
        assert "as_of" in news_issues[0]["issue"]

    def test_error_only_is_reported_even_when_as_of_is_fresh(self, tmp_path, monkeypatch):
        """首次采集就失败：没有旧文件可保护，写了一份带 error 标记、as_of 是
        今天的新文件——as_of 检查逮不到，只能靠 announcements_error 检查报。"""
        self._stocks(tmp_path, monkeypatch, [{"snapshot_key": "300308", "name": "中际旭创"}])
        self._healthy_non_news_files(tmp_path)
        _write(tmp_path, "300308-news.json",
               {"as_of": "2026-08-13", "announcements_error": True})  # 今天，但 error

        issues = rdh.collect_stale(TODAY)

        news_issues = [i for i in issues if i["file"] == "300308-news.json"]
        assert len(news_issues) == 1
        assert "announcements_error" in news_issues[0]["issue"]

    def test_both_stale_and_error_are_reported_together(self, tmp_path, monkeypatch):
        """持续失败的形态：write_and_deploy() 拒绝覆盖旧文件，as_of 停住不动，
        同一只股票同时触发两类检查——这是预期行为，不是重复告警。"""
        self._stocks(tmp_path, monkeypatch, [{"snapshot_key": "300308", "name": "中际旭创"}])
        self._healthy_non_news_files(tmp_path)
        _write(tmp_path, "300308-news.json",
               {"as_of": "2026-08-08", "announcements_error": True})  # 5 天前 + error

        issues = rdh.collect_stale(TODAY)

        news_issues = [i for i in issues if i["file"] == "300308-news.json"]
        assert len(news_issues) == 2
        reasons = "".join(i["issue"] for i in news_issues)
        assert "as_of" in reasons
        assert "announcements_error" in reasons

    def test_fresh_and_no_error_yields_nothing(self, tmp_path, monkeypatch):
        self._stocks(tmp_path, monkeypatch, [{"snapshot_key": "300308", "name": "中际旭创"}])
        self._healthy_non_news_files(tmp_path)
        _write(tmp_path, "300308-news.json",
               {"as_of": "2026-08-13", "announcements_error": False})

        assert rdh.collect_stale(TODAY) == []
