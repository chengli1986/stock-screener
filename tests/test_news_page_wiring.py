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
_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
CLI = FIXTURES / "render_news_cli.js"

PAGES = ["innolight-300308", "cambricon-688256", "shengke-688702", "yuanjie-688498",
         "changguang-688048", "fenghua-000636", "sanhuan-300408", "catl-300750",
         "moutai-600519", "zhipu-02513", "cxmt-ipo"]

# ★F4（2026-08-11 三审 Important）：这两个曾经直接指向 docs-site/data/ 下的
# 活数据（cron 每天重新采集重写）。5 条测试带着数据前提（688048 的
# groups == ["sector"]、announcements_empty；300308 有 procedural 层、有
# group_members 项）——长光华芯一发新公告，这些前提说变就变，测试当天变红，
# 代码却一行没改，是一颗定时红灯。改成读 tests/fixtures/ 下冻结的快照，
# 不再随活数据漂移；快照来源与冻结时间见 fixtures/*-news-snapshot.json 里的
# `_snapshot_note` 字段。
SAMPLE_JSON = FIXTURES / "300308-news-snapshot.json"
EMPTY_ANNOUNCE_JSON = FIXTURES / "688048-news-snapshot.json"  # announcements_empty=true 实例

NODE = shutil.which("node")
skip_no_node = pytest.mark.skipif(NODE is None, reason="node 不在 PATH 上，跳过真实渲染测试")


def render(json_path, patch=None, accord_items=None, sect_nums=None, click=False,
          click_layer=None, click_more_btn=None):
    """调用 render_news_cli.js，真的用 node vm 跑一遍 report-news.js，返回解析后的结果 dict。

    这是本文件里唯一「看真实产出」的入口——所有行为性断言都必须走这里，
    不能退回到对 report-news.js 源码文本做字符串匹配。

    ``click_layer``：Task 7 新增，点击某一层（如 "procedural"）的折叠标题按钮。
    ``click_more_btn``：F1 三审新增，点第 n 个（0-based，文档序）`.rn-more-btn`，
    走 body 级委托（跟 production 一样），不是直接点按钮自己。
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
    if click_more_btn is not None:
        args += ["--click-more-btn", str(click_more_btn)]
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


# ── Fix B（2026-08-12 全量审计）：长江存储（YMTC）页不接 report-news.js ────
#
# ★意图：`ymtc-ipo.html` 是 `#research-watchlist` 12 页里唯一没有「相关新闻
# 与公告」章节的一页——原因是对的（YMTC 尚未上市、没有证券代码，巨潮/东财
# 都无从查起），不该改采集脚本去凑一个假代码，而是页面应该**明说**这个原因，
# 不能留白让读者以为页面坏了。
#
# 这条测试锁住两件必须同时成立的事：
#   ① 页面正文里确实有这段解释（不是留白、也没被后续编辑删掉）；
#   ② `config/research_stocks.json` 确实不含长江存储——这份配置被
#      snapshot/consensus/peers-market 等多个采集脚本共用，一旦长江存储
#      被加进去，那些脚本会全都去抓一个不存在的股票代码。
#
# 两条断言故意绑在一起：哪天长江存储真的上市、被加进 research_stocks.json，
# 这条测试会因为②变红——那是**故意的**，逼着回来把①这段「尚未上市」的
# 说明删掉，不然页面就开始说假话了。反证见 report.md：把②反过来（假装已
# 入池）单独跑一次，必须真的红。
def test_ymtc_page_explains_missing_news_section_and_guards_config():
    html = (DOCS / "pages" / "ymtc-ipo.html").read_text(encoding="utf-8")

    assert 'src="/js/report-news.js"' not in html, (
        "YMTC 尚未上市、无证券代码，不该接 report-news.js 组件——"
        "接了会 fetch 一个不存在的 {key}-news.json")
    assert "尚未上市" in html and "证券代码" in html, (
        "页面正文里没找到『尚未上市/无证券代码』的说明，读者会以为这页少了一节是 bug")

    stocks = json.loads((_ROOT / "config" / "research_stocks.json").read_text(encoding="utf-8"))
    names = {s.get("name") for s in stocks}
    assert "长江存储" not in names, (
        "research_stocks.json 已经把长江存储加进池子了——回到 pages/ymtc-ipo.html "
        "把『尚未上市』那段说明删掉，改接 report-news.js")


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
def test_no_news_layer_page_does_not_promise_related_news_below():
    """★2026-08-11 二审 Finding 3：长光华芯真实数据只剩 sector 一层（news/
    technical 都空了），但旧文案写死「下方为相关新闻」——跟前两轮修的
    announcements_error/empty 是同一类『说了不成立的事』。用真实的
    688048-news.json（此刻 groups 只有 sector 一层）验证不再做这个空头承诺。
    """
    data = json.loads(EMPTY_ANNOUNCE_JSON.read_text(encoding="utf-8"))
    assert [g["layer"] for g in data["groups"]] == ["sector"], (
        "夹具数据不再是『只剩 sector 一层』了，换一个仍符合这个前提的样例，"
        "或直接用 patch 手造一份只有 sector 层的 payload")

    out = render(EMPTY_ANNOUNCE_JSON)

    assert "下方为相关新闻" not in out["html"]
    assert "无公告披露" in out["html"]  # 公告那半句判断不受影响，仍然要在


@skip_no_node
def test_sector_only_page_explains_no_direct_company_news():
    """★Fix C（2026-08-12 全量审计）：688048 长光华芯真实快照只有 sector 层
    （见文件头 F4 说明），此前读者点开只看到「下方为其他相关信息」这句含糊
    话 + 一堆完全没提到长光华芯的板块新闻，会以为页面坏了。新文案必须明说
    『近 N 天没有直接提到本公司的新闻，下方是板块/市场层面的信息』。"""
    data = json.loads(EMPTY_ANNOUNCE_JSON.read_text(encoding="utf-8"))
    assert [g["layer"] for g in data["groups"]] == ["sector"], (
        "夹具数据前提变了，换一个仍是『只剩 sector 一层』的样例")

    out = render(EMPTY_ANNOUNCE_JSON)

    assert "没有直接提到本公司的新闻" in out["html"]
    assert "板块" in out["html"] and "市场" in out["html"]


@skip_no_node
def test_sector_explanation_absent_when_news_layer_present():
    """反面：300308 真实快照有 news 层（见文件头 SAMPLE_JSON），不该出现这句
    『没有直接提到本公司的新闻』——那会是假话，公司本来就有相关新闻。"""
    data = json.loads(SAMPLE_JSON.read_text(encoding="utf-8"))
    assert "news" in [g["layer"] for g in data["groups"]], (
        "夹具数据前提变了，换一个仍带 news 层的样例")

    out = render(SAMPLE_JSON)

    assert "没有直接提到本公司的新闻" not in out["html"]


@skip_no_node
def test_sector_explanation_does_not_duplicate_generic_fallback():
    """新句子上线后，紧邻它的『下方为其他相关信息』泛指半句应该让位，
    不然两句连读会显得啰嗦重复——组合起来必须通顺，不是简单拼接。"""
    out = render(EMPTY_ANNOUNCE_JSON)

    assert "下方为其他相关信息" not in out["html"]
    assert "无公告披露" in out["html"]  # 公告那半句判断不受影响


@skip_no_node
def test_sector_only_notes_are_complete_sentences_no_dangling_punctuation():
    """★Minor（复审 + 独立核查同时发现，2026-08-12 二次修复）：上一版直接把
    收尾半句拼成空字符串，前面那句的句中标点（逗号/分号）却留在了原地——
    「近 30 天无公告披露；」「...暂无法判断窗口内是否有公告，」后面直接接
    `</p>`，句子看起来说了一半，要靠下一段 sectorNote 才能读通。

    两句是两件事（有没有公告 / 有没有本公司新闻），不该合并成一段，但各自
    必须独立成句——句号收口，不依赖后面那段来补完。用字符串精确匹配
    `无公告披露；</p>` / `是否有公告，</p>` 这类悬空标点，直接卡在字符串
    结尾而不是句子中间。"""
    empty_out = render(EMPTY_ANNOUNCE_JSON)
    assert "无公告披露。" in empty_out["html"]
    assert "无公告披露；</p>" not in empty_out["html"]
    assert "无公告披露，</p>" not in empty_out["html"]

    error_out = render(EMPTY_ANNOUNCE_JSON,
                       patch={"announcements_error": True, "announcements_empty": False})
    assert "是否有公告。" in error_out["html"]
    assert "是否有公告，</p>" not in error_out["html"]
    assert "是否有公告；</p>" not in error_out["html"]


@skip_no_node
def test_intro_does_not_hardcode_technical_as_last_layer():
    """★同一处修复：引言曾写死「交易与资金面排在最后」，sector 上线后不再
    成立（11 页里已有多页没有 technical 层，sector 才是实际最后一层）。"""
    out = render(SAMPLE_JSON)

    assert "交易与资金面排在最后" not in out["html"]


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


# ── F1（Critical，2026-08-11 三审，已上线才发现）：聚合展开按钮跨层撞车 ──
#
# `row(it, idx)` 的 idx 此前是每层各自从 0 数起（`(g.items).map(row)` 每层
# 单独 `.map`），但点击委托 `body.querySelector('.rn-members[data-idx="N"]')`
# 是在**整个章节**范围里找。审查者用 Chromium 跑部署副本实测：
# shengke-688702 三层聚合项全是 data-idx="0"，点 substantive 的「共 2 条」，
# 实际展开的是 major 那条。用一个跟审查者复现场景同形状（3 层、每层各一条
# 聚合项）的合成 fixture 验证——不读任何活数据文件，见文件头 F4 关于活数据
# 夹具会漂移的教训，这组新测试从一开始就不踩那个坑。

_MULTI_LAYER_GROUPED_PATCH = {
    "groups": [
        {"layer": "major", "label": "重要事项", "items": [
            {"kind": "announcement", "title": "大事分组示例", "date": "2026-08-10",
             "group_count": 2, "group_members": [
                 {"kind": "announcement", "title": "大事分组示例-历史条目",
                  "date": "2026-08-05", "url": "https://x/major-old"}]},
        ]},
        {"layer": "substantive", "label": "公司公告", "items": [
            {"kind": "announcement", "title": "公告分组示例", "date": "2026-08-10",
             "group_count": 2, "group_members": [
                 {"kind": "announcement", "title": "公告分组示例-历史条目",
                  "date": "2026-08-05", "url": "https://x/sub-old"}]},
        ]},
        {"layer": "procedural", "label": "程序性文件", "items": [
            {"kind": "announcement", "title": "程序分组示例", "date": "2026-08-10",
             "group_count": 2, "group_members": [
                 {"kind": "announcement", "title": "程序分组示例-历史条目",
                  "date": "2026-08-05", "url": "https://x/proc-old"}]},
        ]},
    ],
}


@skip_no_node
def test_cross_layer_more_btn_idx_are_globally_unique():
    """3 层，每层各一条聚合项——F1 修复前这三个按钮的 data-idx 全是 "0"
    （每层独立从 0 数起）。这条断言直接拦撞车：不要求具体是什么值，只要求
    互不相同。"""
    out = render(SAMPLE_JSON, patch=_MULTI_LAYER_GROUPED_PATCH)

    idxs = out["moreBtnIdxs"]
    assert len(idxs) == 3, f"应该有 3 个聚合展开按钮，实际 {idxs}"
    assert len(set(idxs)) == len(idxs), f"data-idx 跨层撞车了: {idxs}"


@skip_no_node
def test_click_second_more_btn_opens_only_its_own_members():
    """★不点第一个（那个永远不会错，见审查者原话），点第二个（substantive
    层那条），断言展开的是它自己对应的 .rn-members，major/procedural 的
    members 仍然保持收起——直接对应审查者的真实复现：点 substantive 的
    「共 2 条」，F1 修复前实际展开的是 major 那条。"""
    out = render(SAMPLE_JSON, patch=_MULTI_LAYER_GROUPED_PATCH, click_more_btn=1)

    by_layer = {m["layer"]: m["open"] for m in out["membersOpen"]}
    assert by_layer == {"major": False, "substantive": True, "procedural": False}, by_layer


@skip_no_node
def test_click_first_more_btn_still_works_as_before():
    """第一个按钮（idx 天然是 0，撞车前后行为一致）也要验证，确保修复没有
    反过来弄坏了最简单的那条路径。"""
    out = render(SAMPLE_JSON, patch=_MULTI_LAYER_GROUPED_PATCH, click_more_btn=0)

    by_layer = {m["layer"]: m["open"] for m in out["membersOpen"]}
    assert by_layer == {"major": True, "substantive": False, "procedural": False}, by_layer


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
