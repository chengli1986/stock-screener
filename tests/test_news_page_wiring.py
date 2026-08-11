#!/usr/bin/env python3
"""test_news_page_wiring.py — 新闻章节的接线守卫

★「算了没接」是本管线犯过多次的错（preferred_stat / momentum / 腾讯码 /
ETNet 逐家明细）。数据产出后没人消费，等于白做——用测试钉住。
"""

from pathlib import Path

DOCS = Path.home() / "docs-site"
PAGES = ["innolight-300308", "cambricon-688256", "shengke-688702", "yuanjie-688498",
         "changguang-688048", "fenghua-000636", "sanhuan-300408", "catl-300750",
         "moutai-600519", "zhipu-02513", "cxmt-ipo"]


def test_component_exists():
    assert (DOCS / "js" / "report-news.js").is_file()


def test_all_eleven_pages_include_it():
    missing = [p for p in PAGES
               if "report-news.js" not in (DOCS / "pages" / f"{p}.html").read_text(encoding="utf-8")]

    assert missing == [], f"这些页没接线: {missing}"


def test_component_reads_the_right_file():
    src = (DOCS / "js" / "report-news.js").read_text(encoding="utf-8")

    assert "-news.json" in src


def test_empty_state_is_explained_not_blank():
    """★今天（2026-08-10）研报页改造的教训：宁可说明原因，也不要留白。
    实测长光华芯近 30 天 0 条公告。"""
    src = (DOCS / "js" / "report-news.js").read_text(encoding="utf-8")

    assert "announcements_empty" in src
    assert "无公告" in src


def test_fetch_failure_is_not_worded_as_zero_announcements():
    """★Task 3 修的 Critical：「抓取失败」与「真的 0 条公告」曾经不可区分，
    页面会打印「近 N 天无公告披露」这句假话。数据端现在给了独立的
    announcements_error 字段（抓取失败，公告层未知），组件必须接住它，
    并且措辞必须与 announcements_empty（抓取成功、确实 0 条）分支不同——
    绝不能对失败态也说「无公告披露」。"""
    src = (DOCS / "js" / "report-news.js").read_text(encoding="utf-8")

    assert "announcements_error" in src

    # 提取 error 分支与 empty 分支各自紧跟的文案字符串，要求二者不同，
    # 且 error 分支的文案不能包含「无公告披露」这句只适用于 empty 分支的话。
    import re

    error_idx = src.index("announcements_error")
    empty_idx = src.index("announcements_empty")

    def following_snippet(idx, span=400):
        return src[idx:idx + span]

    error_snippet = following_snippet(error_idx)
    empty_snippet = following_snippet(empty_idx)

    error_strings = re.findall(r"'([^']*)'", error_snippet)
    empty_strings = re.findall(r"'([^']*)'", empty_snippet)

    error_text = "".join(error_strings)
    empty_text = "".join(empty_strings)

    assert error_text != empty_text, "抓取失败与确实 0 条公告的文案不能相同"
    assert "无公告披露" not in error_text, "抓取失败分支不能说「无公告披露」——公告层其实未知"
