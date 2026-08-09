#!/usr/bin/env python3
"""test_narrative_program_no_hardcode.py — narrative 指令里不许写死能从数据读的值

## 2026-08-09 用户要求

> 「不论怎么说，不能有 hard code 部分，这个很不合理，除非是有意而为之。」

起因：长鑫的 `program.md` 把「仅 3 家机构覆盖（对照宁德 37 家、茅台 46 家）」写死在
指令里，agent 据此推演出「由 3 家增至 4 家」。三个数字里两个是错的——当天修完
覆盖数 bug 后真实值是长鑫 **4** 家、宁德 **39** 家；茅台 46 恰好对上，
又一次印证「主流样本恰好对上会让偏差隐形」。

同一次盘点扫出同类的还有：
- 茅台把一致预期净利写死成 2026E 852.7 / 2027E 894.9 亿并要求「人工同步」，
  实测已漂到 859.9 / 906.9——而 consensus 自 2026-08-08 起**每周一自动刷新**，
  手工副本注定落后。
- 风华 / 三环 / 盛科把 2025A 年报基数写死当 TTM 分母（2.83 / 57.56 / 26.18 /
  90.07 / 11.51 亿）。这些值今天是对的，但 **2026 年报一发布，分母会静默停在
  2025A 而市值是新的**——算出来的 PE-TTM 凭空变大且无人察觉。

## 什么是「有意而为之」，本测试不碰

情景假设与目标倍数（基准净利 45/285/920 亿、目标 PE 25/35/50x、正常化上沿
15,840 亿等）是**报告自己的立场锚**，被刻意冻结，防止周更 agent 漂移投资结论；
程序里都明写「固定不得改动，只能由人工修改本 program」。它们不是数据副本。

净资产常量（风华 125.11 / 三环 216.59 亿）是**已知缺口**：financials.json
没有净资产字段，暂时只能写死，程序里已标注「不是有意为之」。
补上数据源后应一并解除，届时本测试要加上对应断言。
"""

import json
import re
from pathlib import Path

import pytest

DOCS = Path.home() / "docs-site"
PROGRAMS = sorted((DOCS / "scripts").glob("*-narrative/program.md"))
DATA = DOCS / "data"

# program 目录名 → snapshot key
CODE = {"cambricon": "688256", "catl": "300750", "changguang": "688048",
        "cxmt": "688825", "fenghua": "000636", "innolight": "300308",
        "moutai": "600519", "sanhuan": "300408", "shengke": "688702",
        "yuanjie": "688498", "zhipu": "02513"}


def _key(p: Path) -> str:
    return CODE[p.parent.name.replace("-narrative", "")]


def _json(name: str):
    f = DATA / name
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def test_every_program_is_mapped():
    """新增标的忘了登记，后面所有断言会静默跳过它。"""
    assert {p.parent.name.replace("-narrative", "") for p in PROGRAMS} == set(CODE)


@pytest.mark.parametrize("prog", PROGRAMS, ids=lambda p: p.parent.name)
def test_no_hardcoded_broker_count(prog):
    """★覆盖机构数必须现读。写死的那份当场就有两个是错的。

    池内**任何**标的的家数出现在**任何** program 里都算违规——长鑫那句写死的
    正是别人家的家数（宁德 37）。
    """
    text = prog.read_text(encoding="utf-8")
    for key in CODE.values():
        c = _json(f"{key}-consensus.json")
        for y, bs in (c.get("broker_stats") or {}).items():
            n = bs.get("coverage_orgs")
            if n and re.search(r"(?<![\d.])" + str(n) + r"\s*家", text):
                pytest.fail(f"{prog.parent.name} 写死了 {key} {y} 的覆盖机构数 {n} 家")


@pytest.mark.parametrize("prog", PROGRAMS, ids=lambda p: p.parent.name)
def test_no_hardcoded_annual_base(prog):
    """★年报基数必须现读——写死的话 2026 年报一发布，TTM 分母静默停在 2025A。"""
    text = prog.read_text(encoding="utf-8")
    la = _json(f"{_key(prog)}-financials.json").get("latest_annual") or {}
    for field in ("revenue_yi", "profit_yi"):
        v = la.get(field)
        if v is None:
            continue
        lit = f"{v:.2f}".rstrip("0").rstrip(".")
        # 只查两位小数的字面量：整数太容易和情景假设、字数上限撞车
        if "." in lit and re.search(r"(?<![\d.])" + re.escape(lit) + r"(?![\d])", text):
            pytest.fail(f"{prog.parent.name} 写死了年报 {field}={lit}，应读 latest_annual")


@pytest.mark.parametrize("prog", PROGRAMS, ids=lambda p: p.parent.name)
def test_no_hardcoded_consensus_value(prog):
    """★一致预期每周一自动刷新，program 里的手工副本注定落后（茅台实测已漂）。"""
    text = prog.read_text(encoding="utf-8")
    est = _json(f"{_key(prog)}-consensus.json").get("estimates") or {}
    for y, e in est.items():
        for field in ("profit_yuan", "revenue_yuan"):
            v = e.get(field)
            if not v or v <= 0:
                continue
            lit = f"{v / 1e8:.1f}"
            if re.search(r"(?<![\d.])" + re.escape(lit) + r"\s*亿", text):
                pytest.fail(f"{prog.parent.name} 写死了 {y} {field}={lit} 亿，应现读 consensus")


@pytest.mark.parametrize("prog", PROGRAMS, ids=lambda p: p.parent.name)
def test_ttm_denominator_reads_latest_annual(prog):
    """正面断言：凡是算 TTM 倍数的 program，都必须提到 latest_annual。

    只删掉写死的数字、忘了写「从哪读」，agent 会自己去编一个分母。
    """
    text = prog.read_text(encoding="utf-8")
    if not re.search(r"(pe_ttm|ps_ttm|ps_2025a)\s*=", text):
        pytest.skip("本 program 不算 TTM 倍数")

    assert "latest_annual" in text, f"{prog.parent.name} 算 TTM 却没说分母从哪读"


@pytest.mark.parametrize("prog", PROGRAMS, ids=lambda p: p.parent.name)
def test_coverage_orgs_not_count(prog):
    """提机构数的 program 必须点名 coverage_orgs——`count` 是东财明细条数，
    永远更小（实测茅台 46 vs 31、宁德 39 vs 27、长鑫 4 vs 2）。"""
    text = prog.read_text(encoding="utf-8")
    if "家机构" not in text:
        pytest.skip("本 program 不提机构数")

    assert "coverage_orgs" in text, f"{prog.parent.name} 提机构数却没指明用 coverage_orgs"


@pytest.mark.parametrize("prog", PROGRAMS, ids=lambda p: p.parent.name)
def test_placeholders_resolve_to_something_the_program_reads(prog):
    """★占位符必须有出处：`{coverage_orgs}` 这类模板变量若在 Phase 1 里没读过，
    agent 只能猜——本次修长鑫时就差点留下一个没定义的 last_coverage_orgs。"""
    text = prog.read_text(encoding="utf-8")
    for ph in set(re.findall(r"\{([a-z][a-z0-9_]{3,})\}", text)):
        if ph in ("today", "iso8601_bjt"):
            continue
        # 派生写法（update_reason_brief ← update_reason）算有出处：去掉末段后缀再找一次
        base = ph.rsplit("_", 1)[0]
        ok = text.count(ph) >= 2 or (len(base) > 3 and base in text.replace(ph, ""))

        assert ok, f"{prog.parent.name} 的占位符 {{{ph}}} 只出现一次，无出处"
