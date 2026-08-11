#!/usr/bin/env python3
"""test_news_page_wiring.py — 新闻章节的接线守卫

★「算了没接」是本管线犯过多次的错（preferred_stat / momentum / 腾讯码 /
ETNet 逐家明细）。数据产出后没人消费，等于白做——用测试钉住。

★2026-08-11 二审教训：本文件第一版只有源码文本断言（`"announcements_error"
in src`），审查用两个变异体证明它们是瞎子——① 把 error 分支文案换成
「无公告披露」这句假话、② 把整个 error 分支删掉只留注释里的字样——两次都
全绿。源码文本断言测的是「这个词出现在文件里某处」，不是「这段逻辑真的
执行、真的产出了这段话」，对付不了「文案挪了位置」「分支被删」这类改动。

修法：`tests/fixtures/render_news.js` + `render_news_cli.js` 用 node
`vm` 模块真的跑一遍 `docs-site/js/report-news.js`（一个手搓的最小 DOM
shim，不需要 jsdom），本文件用 subprocess 调用、断言的是**真实渲染出的
HTML 字符串**和**真实的 click 行为**，不是源码文本。原来的源码文本断言
保留在下面当「廉价冒烟」，但不再是唯一防线。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DOCS = Path.home() / "docs-site"
FIXTURES = Path(__file__).parent / "fixtures"
CLI = FIXTURES / "render_news_cli.js"

PAGES = ["innolight-300308", "cambricon-688256", "shengke-688702", "yuanjie-688498",
         "changguang-688048", "fenghua-000636", "sanhuan-300408", "catl-300750",
         "moutai-600519", "zhipu-02513", "cxmt-ipo"]

# 真实抓取产出的样例数据，Task 3/4 的产物，用作渲染冒烟测试的输入。
SAMPLE_JSON = DOCS / "data" / "300308-news.json"
EMPTY_ANNOUNCE_JSON = DOCS / "data" / "688048-news.json"  # announcements_empty=true 实例

NODE = shutil.which("node")
skip_no_node = pytest.mark.skipif(NODE is None, reason="node 不在 PATH 上，跳过真实渲染测试")


def render(json_path, patch=None, accord_items=None, sect_nums=None, click=False,
          click_layer=None):
    """调用 render_news_cli.js，真的用 node vm 跑一遍 report-news.js，返回解析后的结果 dict。

    这是本文件里唯一「看真实产出」的入口——所有行为性断言都必须走这里，
    不能退回到对 report-news.js 源码文本做字符串匹配。

    ``click_layer``：Task 7 新增，点击某一层（如 "procedural"）的折叠标题按钮。
    """
    args = [NODE, str(CLI), str(json_path)]
    if patch is not None:
        args += ["--patch", json.dumps(patch, ensure_ascii=False)]
    if accord_items is not None:
        args += ["--accord-items", ",".join(accord_items)]
    if sect_nums is not None:
        args += ["--sect-nums", ",".join(sect_nums)]
    if click:
        args += ["--click"]
    if click_layer is not None:
        args += ["--click-layer", click_layer]
    r = subprocess.run(args, capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"render_news_cli.js 非 0 退出: {r.stdout} {r.stderr}"
    out = json.loads(r.stdout)
    assert "error" not in out, f"render_news_cli.js 内部报错: {out['error']}"
    return out


# ── 廉价冒烟（保留，但不再是唯一防线，见文件头注释）──────────────────────

def test_component_exists():
    assert (DOCS / "js" / "report-news.js").is_file()


def test_all_eleven_pages_include_it():
    missing = [p for p in PAGES
               if "report-news.js" not in (DOCS / "pages" / f"{p}.html").read_text(encoding="utf-8")]

    assert missing == [], f"这些页没接线: {missing}"


def test_component_reads_the_right_file():
    src = (DOCS / "js" / "report-news.js").read_text(encoding="utf-8")

    assert "-news.json" in src


def test_source_mentions_both_announcement_flags():
    """廉价冒烟：两个字段名至少都要出现在源码里。真正的行为断言在
    test_fetch_failure_is_worded_differently_from_zero_announcements 里。"""
    src = (DOCS / "js" / "report-news.js").read_text(encoding="utf-8")

    assert "announcements_empty" in src
    assert "announcements_error" in src


def test_no_inline_onclick_toggle():
    """C1 回归哨兵：本节头部按钮不得再用 `onclick="toggle(this)"` 这种依赖页面
    全局函数的写法——innolight-300308 页全局根本没有 toggle()，点击直接
    ReferenceError，2026-08-11 审查发现时线上已经是坏的。真实行为断言见
    test_accordion_click_toggles_without_any_page_global。"""
    src = (DOCS / "js" / "report-news.js").read_text(encoding="utf-8")

    assert 'onclick="toggle' not in src
    assert "addEventListener('click'" in src or 'addEventListener("click"' in src


# ── 真实渲染 / 真实行为断言（node vm，非源码文本匹配）────────────────────

@skip_no_node
def test_is_empty_renders_nothing():
    """d.is_empty === true → 不渲染空壳章节，.page-wrap 不应多出任何子元素。
    这条此前完全没有测试覆盖（自查时删掉早退守卫，全部测试仍然绿）。"""
    out = render(SAMPLE_JSON, patch={"is_empty": True})

    assert out["childrenCount"] == 0


@skip_no_node
def test_announcements_error_is_worded_as_failure_not_zero():
    """★Task 3 修的 Critical，本节是修复链最后一环：抓取失败（公告层未知）绝不能
    说成「无公告披露」（那是抓取成功且确实 0 条才能说的话）。"""
    out = render(SAMPLE_JSON, patch={"announcements_error": True, "announcements_empty": False})

    assert "更新失败" in out["html"]
    assert "无公告" not in out["html"]


@skip_no_node
def test_announcements_empty_is_worded_as_zero():
    """抓取成功、窗口内确实 0 条 → 可以说「无公告披露」。用长光华芯真实抓取产出
    （announcements_empty=true）验证，不用手造的 patch。"""
    out = render(EMPTY_ANNOUNCE_JSON)

    assert "无公告披露" in out["html"]


@skip_no_node
def test_fetch_failure_is_worded_differently_from_zero_announcements():
    """两个分支的文案必须不同——这条直接对应 2026-08-11 审查用来证伪上一版测试的
    变异体①（把 error 分支文案换成 empty 分支那句假话）。"""
    error_out = render(SAMPLE_JSON, patch={"announcements_error": True, "announcements_empty": False})
    empty_out = render(EMPTY_ANNOUNCE_JSON)

    assert error_out["html"] != empty_out["html"]
    assert "更新失败" in error_out["html"]
    assert "无公告披露" in empty_out["html"]
    assert "无公告披露" not in error_out["html"]
    assert "更新失败" not in empty_out["html"]


@skip_no_node
def test_accordion_click_toggles_without_any_page_global():
    """C1 修复的真实行为验证：点击头部按钮，data-open 与 body 的 open class
    必须真的翻转——且整个过程不依赖任何页面全局 toggle() 或 data-target
    查找（render_news.js 的 shim 里根本没有提供这两样东西，能翻转就说明
    组件是自给自足的）。"""
    before = render(SAMPLE_JSON, click=False)
    after = render(SAMPLE_JSON, click=True)

    assert before["btnDataOpen"] == "false"
    assert before["bodyOpen"] is False
    assert after["btnDataOpen"] == "true"
    assert after["bodyOpen"] is True


@skip_no_node
def test_section_inserted_after_last_accord_item_not_after_disclaimer():
    """M7 回归哨兵：`.accord-wrap` 全仓不存在，此前死代码分支导致本节总是落到
    `.page-wrap` 末尾——如果末尾是 `<div class="disclaimer">`（innolight-300308
    的真实结构），本节就被插到了免责声明下面。模拟同样的结构，断言插入点在
    最后一个 .accord-item 之后、disclaimer 之前。"""
    out = render(SAMPLE_JSON, accord_items=["accord-item", "accord-item", "disclaimer"])

    assert out["children"] == ["accord-item", "accord-item", "accord-item rn-sec", "disclaimer"]


@skip_no_node
def test_section_falls_back_to_page_wrap_end_when_no_accord_item():
    out = render(SAMPLE_JSON, accord_items=[])

    assert out["children"] == ["accord-item rn-sec"]


@skip_no_node
def test_sect_num_continues_from_last_number_not_badge_count():
    """M6 回归哨兵：cxmt-ipo 的徽章是 ['01',...,'03b',...,'10']——11 个徽章、
    末号却是 10（有跳号）。此前用「个数 + 1」算出 12，跳过了 11。"""
    out = render(SAMPLE_JSON, sect_nums=["01", "02", "03", "03b", "04", "05", "06", "07", "08", "09", "10"])

    assert '<span class="sect-num">11</span>' in out["btnHtml"]
    assert '<span class="sect-num">12</span>' not in out["btnHtml"]


@skip_no_node
def test_sect_num_defaults_to_section_style_when_no_badges_exist():
    """M9 回归哨兵：没有任何既有徽章时退化成 '§1'（多数页风格），不是 cxmt-ipo
    那种两位数字风格的 '01'。"""
    out = render(SAMPLE_JSON, sect_nums=[])

    assert '<span class="sect-num">§1</span>' in out["btnHtml"]


@skip_no_node
def test_non_http_url_degrades_to_plain_text():
    """I4：href 协议白名单。采集侧目前对巨潮/东财/披露易/yfinance 的链接没有做
    scheme 校验（已 grep 确认零命中），esc() 只挡引号/尖括号，挡不住
    `javascript:` 之类伪协议。现存 208 条 URL 全是 http/https，这里用手造
    的恶意 URL 验证兜底：非 http(s) 直接退化成纯文本，不当 href 输出。"""
    out = render(SAMPLE_JSON, patch={
        "groups": [{
            "layer": "news", "label": "t",
            "items": [{"kind": "news", "title": "evil", "date": "2026-08-11",
                       "url": "javascript:alert(1)", "group_count": 1}],
        }],
    })

    assert "javascript:" not in out["html"]
    assert "<a " not in out["html"]
    assert "evil" in out["html"]


@skip_no_node
def test_group_members_are_reachable_in_output():
    """I5：group_members 存了没人看，是同一类「preferred_stat / momentum /
    腾讯码 / ETNet 逐家明细」的账——11 份 JSON 实测 208 条里有 14 条聚合项、
    共 29 条 group_members 完全无路可达。300308 的真实数据里就有一条
    group_count=2 的聚合项，其 group_members[0].title 必须出现在渲染输出里
    （渲染成默认折叠、点击展开的嵌套列表，不是完全不渲染）。"""
    data = json.loads(SAMPLE_JSON.read_text(encoding="utf-8"))
    member_title = None
    for g in data["groups"]:
        for it in g["items"]:
            members = it.get("group_members") or []
            if members:
                member_title = members[0]["title"]
                break
        if member_title:
            break
    assert member_title, "夹具数据里没有带 group_members 的条目了，换一只股票的样例"

    out = render(SAMPLE_JSON)

    assert member_title in out["html"]
    assert 'class="rn-members"' in out["html"]
    assert 'rn-more-btn' in out["html"]


# ── Task 7：procedural / sector 默认折叠 ────────────────────────────────────
#
# ★这个组件的测试必须是行为测试，不能是源码文本断言——上一轮教训见文件头。
# 下面用 render_news_cli.js 新增的 `layers` 字段（真实 <ul> 的 classList 状态）
# 和 `--click-layer` 断言，不对 report-news.js 源码做字符串匹配。

_ALL_LAYERS_PATCH = {
    "groups": [
        {"layer": "major", "label": "重要事项",
         "items": [{"kind": "announcement", "title": "大事一条", "date": "2026-08-10",
                    "group_count": 1}]},
        {"layer": "substantive", "label": "公司公告",
         "items": [{"kind": "announcement", "title": "公告一条", "date": "2026-08-10",
                    "group_count": 1}]},
        {"layer": "news", "label": "相关新闻",
         "items": [{"kind": "news", "title": "新闻一条", "date": "2026-08-10",
                    "group_count": 1}]},
        {"layer": "procedural", "label": "程序性文件",
         "items": [{"kind": "announcement", "title": "程序一", "date": "2026-08-10",
                    "group_count": 1},
                   {"kind": "announcement", "title": "程序二", "date": "2026-08-09",
                    "group_count": 1}]},
        {"layer": "technical", "label": "交易与资金面",
         "items": [{"kind": "news", "title": "龙虎榜一条", "date": "2026-08-10",
                    "group_count": 1}]},
        {"layer": "sector", "label": "板块与市场",
         "items": [{"kind": "news", "title": "板块一", "date": "2026-08-10",
                    "group_count": 1},
                   {"kind": "news", "title": "板块二", "date": "2026-08-09",
                    "group_count": 1}]},
    ],
}


@skip_no_node
def test_procedural_and_sector_collapsed_by_default_others_expanded():
    """★用户 2026-08-11 拍板：procedural 默认收起（占篇幅、淡化实质内容）；
    sector 层体量同样大、价值更低，同一条理由适用，同样默认收起
    （controller 判定，已在报告里向用户说明）。其余四层保持展开。"""
    out = render(SAMPLE_JSON, patch=_ALL_LAYERS_PATCH)

    assert out["layers"]["procedural"]["open"] is False
    assert out["layers"]["sector"]["open"] is False
    assert "rn-collapsible" in out["layers"]["procedural"]["className"]
    assert "rn-collapsible" in out["layers"]["sector"]["className"]

    for layer in ("major", "substantive", "news", "technical"):
        assert "rn-collapsible" not in out["layers"][layer]["className"], (
            f"{layer} 不该带折叠类——用户只要求 procedural/sector 默认收起")

    # 折叠层的条目内容仍然真实渲染在输出里，只是靠 CSS class 收起，不是没渲染。
    assert "程序一" in out["html"]
    assert "板块一" in out["html"]


@skip_no_node
def test_procedural_header_shows_count_and_collapsed_arrow():
    out = render(SAMPLE_JSON, patch=_ALL_LAYERS_PATCH)

    assert "程序性文件（共 2 条）" in out["html"]
    assert "板块与市场（共 2 条）" in out["html"]


@skip_no_node
def test_click_on_procedural_header_expands_only_procedural():
    """点击 procedural 的标题行后该层展开；没点的 sector 仍然保持收起——
    证明每层的 click 监听各自独立，不是全局一个开关。"""
    before = render(SAMPLE_JSON, patch=_ALL_LAYERS_PATCH)
    after = render(SAMPLE_JSON, patch=_ALL_LAYERS_PATCH, click_layer="procedural")

    assert before["layers"]["procedural"]["open"] is False
    assert after["layers"]["procedural"]["open"] is True
    assert after["layers"]["sector"]["open"] is False


@skip_no_node
def test_procedural_click_toggles_using_real_fixture_data():
    """不用手造 patch，直接用 300308 真实抓取里的 procedural 层验证同样的行为。"""
    data = json.loads(SAMPLE_JSON.read_text(encoding="utf-8"))
    assert any(g["layer"] == "procedural" for g in data["groups"]), \
        "夹具数据里没有 procedural 层了，换一只股票的样例"

    before = render(SAMPLE_JSON)
    after = render(SAMPLE_JSON, click_layer="procedural")

    assert before["layers"]["procedural"]["open"] is False
    assert after["layers"]["procedural"]["open"] is True


def test_no_hardcoded_yellow_hex_left():
    """M8：改用页面已有 --yellow 变量，不再硬编码 #f59e0b。"""
    src = (DOCS / "js" / "report-news.js").read_text(encoding="utf-8")

    assert "#f59e0b" not in src
    assert "var(--yellow)" in src
