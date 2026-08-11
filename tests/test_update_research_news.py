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


# ── 公告抓取失败 ≠ 确认 0 条（审查 Finding 1）───────────────────────────────
#
# ★调用链：cninfo 抛异常 → main 接住 → 若把 anns 设为 []，则 build_payload
# 无法区分「真的 0 条」与「没抓到」，会把 announcements_empty 写成 True，
# 页面据此打印「近 30 天无公告披露」——这是一句假话。announcements=None 是
# 这个区分的哨兵。


class TestAnnouncementsFetchFailure:
    def test_failed_fetch_sets_error_flag_not_empty_claim(self):
        """公告抓取失败（None）+ 新闻抓取成功 → 不能说「无公告披露」。"""
        p = urn.build_payload("688498", "源杰科技", None,
                              [news("公司获大单", "2026-08-05")], NOW)

        assert p["announcements_error"] is True
        assert p["announcements_empty"] is False

    def test_confirmed_zero_announcements_sets_empty_not_error(self):
        """公告抓取成功、窗口内确认 0 条 → 是空态，不是错误。"""
        p = urn.build_payload("688048", "长光华芯", [], [], NOW)

        assert p["announcements_empty"] is True
        assert p["announcements_error"] is False

    def test_error_payload_does_not_overwrite_existing_file(self, tmp_path):
        """★announcements_error 为真时，即便 payload 整体不是「空」（新闻还在），
        也不能覆盖昨天那份含公告的好文件。"""
        target = tmp_path / "688498-news.json"
        target.write_text(
            json.dumps({"groups": [{"layer": "substantive", "items": [1]}]}),
            encoding="utf-8")
        payload = {"is_empty": False, "announcements_error": True,
                   "groups": [{"layer": "news", "items": [{"title": "某新闻"}]}]}

        urn.write_and_deploy("688498", payload, tmp_path, None)

        assert json.loads(target.read_text())["groups"][0]["layer"] == "substantive"


# ── 空结果 KeyError ≠ 真故障（二次审查 问题 1）──────────────────────────────
#
# ★实测（不是推测）：688048 长光华芯近 30 天真的 0 条公告，akshare 对这个
# 空结果窗口抛 KeyError（内部试图对空 DataFrame 选列）；近 90/180/365 天都
# 能正常返回。同一只股票、同一个函数，窄窗异常、宽窗正常——异常本身无法
# 区分「真故障」与「真的没有」。捕获 KeyError 就当 0 条会在 schema 真变了
# 时静默报错误结论，这几条测试专门锁住「先扩窗自检，再决定」这个行为。


class TestCninfoEmptyResultVsBrokenFetch:
    def test_narrow_keyerror_wide_succeeds_returns_empty_not_raises(self, monkeypatch):
        """窄窗 KeyError + 扩窗成功 → 真·0 条，返回 []，不再往上抛异常。"""
        import akshare as ak

        calls = []

        def fake(symbol, market, start_date, end_date):
            calls.append((start_date, end_date))
            if len(calls) == 1:
                raise KeyError(
                    "None of [Index(['代码','简称','公告标题','公告时间',"
                    "'announcementId','orgId'], dtype='str')] are in the [columns]")
            import pandas as pd
            return pd.DataFrame(columns=["代码", "简称", "公告标题", "公告时间", "公告链接"])

        monkeypatch.setattr(ak, "stock_zh_a_disclosure_report_cninfo", fake)

        got = urn.fetch_cninfo_announcements("688048")

        assert got == []
        assert len(calls) == 2  # 窄窗一次 + 扩窗自检一次

    def test_narrow_and_wide_both_keyerror_raises(self, monkeypatch):
        """窄窗、扩窗都 KeyError → 真的坏了，原样抛出（上层据此置 None 哨兵）。"""
        import akshare as ak

        def always_broken(symbol, market, start_date, end_date):
            raise KeyError("schema 真的变了")

        monkeypatch.setattr(ak, "stock_zh_a_disclosure_report_cninfo", always_broken)

        try:
            urn.fetch_cninfo_announcements("688048")
        except KeyError:
            pass
        else:
            raise AssertionError("应抛出 KeyError，而不是静默返回")

    def test_normal_path_does_not_trigger_probe(self, monkeypatch):
        """★正常路径零开销：不该多打一次扩窗请求。"""
        import akshare as ak

        calls = []

        def fake(symbol, market, start_date, end_date):
            calls.append((start_date, end_date))
            import pandas as pd
            return pd.DataFrame([{"代码": symbol, "简称": "中际旭创",
                                  "公告标题": "关于董事会换届的公告",
                                  "公告时间": "2026-08-01", "公告链接": "http://x"}])

        monkeypatch.setattr(ak, "stock_zh_a_disclosure_report_cninfo", fake)

        got = urn.fetch_cninfo_announcements("300308")

        assert len(got) == 1
        assert len(calls) == 1  # 没有触发扩窗自检


# ── 历史文件里的坏行不该拖垮整只股票（审查 Finding 2）───────────────────────


class TestHistoryCorruptRows:
    def test_non_dict_json_row_is_skipped_not_fatal(self, tmp_path):
        """★实测触发路径：历史 jsonl 里一行合法 JSON 但不是 dict（如裸数字），
        `r.get("url")` 会抛 AttributeError。该行应被跳过而不是让整个调用炸掉。"""
        path = tmp_path / "300308-news-raw.jsonl"
        path.write_text("123\n" + json.dumps(news("A", "2026-08-01")) + "\n",
                        encoding="utf-8")

        got = urn.merge_news_history("300308", [], tmp_path, now=NOW)

        assert len(got) == 1
        assert got[0]["title"] == "A"


# ── 港股：披露易解析 ─────────────────────────────────────────────────────────


class TestHkexParse:
    """★披露易返回的日期是 DD/MM/YYYY，且 SHORT_TEXT 自带官方分类。
    日期格式弄反会让全部条目掉出 30 天窗口——静默失败，页面只是空着。"""

    _ROWS = [{"DATE_TIME": "04/08/2026 16:30", "STOCK_CODE": "02513",
              "SHORT_TEXT": "月報表<br/>",
              "TITLE": "截至二零二六年七月三十一日止月份之股份發行人的證券變動月報表",
              "FILE_LINK": "/listedco/listconews/sehk/2026/0804/2026080400123.pdf"},
             {"DATE_TIME": "13/07/2026 08:00", "STOCK_CODE": "02513",
              "SHORT_TEXT": "公告及通告 - [配售]<br/>",
              "TITLE": "完成根據一般授權配售新H股",
              "FILE_LINK": "/listedco/listconews/sehk/2026/0713/2026071300456.pdf"}]

    def test_date_converted_to_iso(self):
        got = urn.parse_hkex_rows(self._ROWS)

        assert got[0]["date"] == "2026-08-04"

    def test_category_kept_for_classification(self):
        got = urn.parse_hkex_rows(self._ROWS)

        assert got[0]["category"] == "月報表"

    def test_url_is_absolute(self):
        """FILE_LINK 是相对路径，直接给页面会点不开。"""
        got = urn.parse_hkex_rows(self._ROWS)

        assert got[0]["url"].startswith("https://www1.hkexnews.hk/")

    def test_html_break_stripped_from_category(self):
        got = urn.parse_hkex_rows(self._ROWS)

        assert "<br/>" not in got[1]["category"]

    def test_kind_is_announcement(self):
        got = urn.parse_hkex_rows(self._ROWS)

        assert all(x["kind"] == "announcement" for x in got)


class TestHkClassificationEndToEnd:
    """港股公告经 parse → classify 后应落到正确的层。"""

    def test_monthly_return_is_procedural(self):
        got = urn.parse_hkex_rows(TestHkexParse._ROWS)
        p = urn.build_payload("02513", "智谱", got, [], "2026-08-11")
        layers = {g["layer"] for g in p["groups"]}

        assert "procedural" in layers

    def test_placing_is_substantive(self):
        got = urn.parse_hkex_rows(TestHkexParse._ROWS)
        p = urn.build_payload("02513", "智谱", got, [], "2026-08-11")
        subs = [g for g in p["groups"] if g["layer"] == "substantive"]

        assert subs and "配售" in subs[0]["items"][0]["title"]


# ── 港股：审查 Finding 1 — yfinance ticker 推导 ─────────────────────────────
#
# ★`symbol.lstrip('0')` 只在「去零后仍 ≥4 位」时碰巧对。审查者实测：
# 腾讯 00700 → lstrip 推导 700.HK（错，应为 0700.HK）；汇丰 00005 → 5.HK
# （错，应为 0005.HK）；yfinance 对错误 ticker 不抛异常、静默返回空 news，
# 池子里将来加入这类代码只会得到一个空新闻层，没有告警可查。


class TestHkYfTicker:
    def test_four_digit_code_unchanged(self):
        assert urn.hk_yf_ticker("02513") == "2513.HK"

    def test_short_code_padded_not_truncated(self):
        """★这是 Finding 1 的核心断言：lstrip('0') 会把 00700 错推成 700.HK。"""
        assert urn.hk_yf_ticker("00700") == "0700.HK"

    def test_single_digit_code_padded(self):
        assert urn.hk_yf_ticker("00005") == "0005.HK"

    def test_five_digit_code_unchanged(self):
        assert urn.hk_yf_ticker("09988") == "9988.HK"


# ── 港股：审查 Finding 2 — rowRange=100 截断检测 ────────────────────────────


class TestHkexTruncationDetection:
    def test_has_next_row_true_is_truncated(self):
        assert urn._hkex_truncated({"hasNextRow": True, "recordCnt": 137}) is True

    def test_has_next_row_string_true_is_truncated(self):
        """披露易可能把布尔值序列化成字符串，两种形式都要认。"""
        assert urn._hkex_truncated({"hasNextRow": "true"}) is True

    def test_has_next_row_false_is_not_truncated(self):
        assert urn._hkex_truncated({"hasNextRow": False, "recordCnt": 5}) is False

    def test_missing_has_next_row_is_not_truncated(self):
        assert urn._hkex_truncated({}) is False


# ── 港股：审查 Finding 3 — 日期解析失败不再静默丢弃 ──────────────────────────
#
# ★审查者实测：3 行输入（正常 / DATE_TIME 为空 / ISO 格式）→ 输出 1 行，
# 丢了 2 条、零提示。丢弃后公告数变少但 announcements_error 仍是 False，
# 页面会正常显示一个不完整的列表——这是 Task 3 那条 Critical 的同类问题。


class TestHkexMalformedRows:
    def test_malformed_rows_skipped_not_fatal(self):
        rows = list(TestHkexParse._ROWS) + [
            {"DATE_TIME": "", "STOCK_CODE": "02513", "SHORT_TEXT": "x",
             "TITLE": "空日期", "FILE_LINK": "/a"},
            {"DATE_TIME": "2026-08-04 16:30", "STOCK_CODE": "02513", "SHORT_TEXT": "x",
             "TITLE": "ISO格式", "FILE_LINK": "/b"},
        ]

        got = urn.parse_hkex_rows(rows)  # 不应抛异常

        assert len(got) == 2  # 原 2 条正常行保留，2 条畸形行被跳过

    def test_all_rows_malformed_returns_empty_not_raises(self):
        rows = [{"DATE_TIME": "not-a-date", "TITLE": "x", "FILE_LINK": "/a"}]

        assert urn.parse_hkex_rows(rows) == []


# ── 港股：审查 Finding 4 — HTML 实体未反转义 ────────────────────────────────
#
# ★实测线上 02513-news.json 落盘了 "&#x2f;"（应为「/」）——目前没有页面消费
# category 字段所以还看不见，Task 5 若改用 textContent 渲染会露出乱码。


class TestHkexHtmlEntities:
    def test_html_entity_unescaped_in_category(self):
        rows = [{"DATE_TIME": "13/07/2026 08:00", "STOCK_CODE": "02513",
                 "SHORT_TEXT": "公告及通告 - [配售 &#x2f; 根據一般性授權發行股份]<br/>",
                 "TITLE": "完成根據一般授權配售新H股", "FILE_LINK": "/a"}]

        got = urn.parse_hkex_rows(rows)

        assert got[0]["category"] == "公告及通告 - [配售 / 根據一般性授權發行股份]"
