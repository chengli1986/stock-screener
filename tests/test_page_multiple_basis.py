#!/usr/bin/env python3
"""test_page_multiple_basis.py — 页面倍数的口径，必须与页面自己的标注一致

## 2026-08-09 用户决定：维持同花顺全覆盖均值（方案 A）

选项 A/B/C 摆出来后用户选 A。A 是自洽的：页面显示「覆盖 15 家」，倍数也来自
这同一批 15 家的同花顺汇总均值；切到东财明细的截尾均值反而会自相矛盾
（那是另一批 10 家）。

## 但 A 暴露了一处页面在说谎

可信度条原先印「统计口径：截尾均值（剔除两端极值后取平均）」和
「离群机构：已在截尾时剔除」——这两句描述的是 `broker_stats.preferred_stat`，
即**东财逐家明细子集**的统计量。而页面 PE 用的是同花顺全覆盖**算术**均值，
**一个极值都没剔除**。读者看到的是「去极值后的 PE」，实际拿到的不是。

寒武纪 2027E 最能说明代价：同花顺 14 家、区间 76~**261** 亿（最高是最低的 3.4 倍），
东财明细 10 家、区间 81~128 亿——**那个 261 亿的预测在东财明细里根本不存在**，
我们连是哪家给的都不知道，而它就在页面 66.0x 的分母里。

## 顺带厘清：这里其实有四个数（曾被我说成「两种算法」）

寒武纪 2027E 净利：
  ① 同花顺汇总      114.2 亿（14 家）← 页面倍数用这个
  ② 东财汇总        103.9 亿（推断 13 家）← cross_check 拿它跟①比，−9.0%
  ③ 东财明细均值     97.3 亿（10 家）
  ④ 东财明细截尾均值 95.5 亿
①②③④ 两两之间的差，大头是**换样本**不是换算法（截尾本身多数 <2.5%）。
⚠ ② 与 ① 差 −9.0%，距 DIVERGENT 阈值只有 1 个百分点——越线就会触发背离告警。
"""

import json
import re
from pathlib import Path

import pytest

DOCS = Path.home() / "docs-site"
DATA = DOCS / "data"
CQ_JS = DOCS / "js" / "consensus-quality.js"
KEYS = sorted(p.name.split("-")[0] for p in DATA.glob("*-consensus.json"))


def _load(key):
    return (json.loads((DATA / f"{key}-snapshot.json").read_text()),
            json.loads((DATA / f"{key}-consensus.json").read_text()))


def test_keys_found():
    assert len(KEYS) == 11


@pytest.mark.parametrize("key", KEYS)
def test_page_multiple_uses_ths_full_coverage_mean(key):
    """★钉住方案 A：页面倍数 = 市值 ÷ 同花顺全覆盖均值，不是东财明细截尾均值。

    若哪天有人改成 preferred_value，本测试会红——那是个需要重新讨论的决定，
    不该由某次重构顺手改掉。
    """
    snap, cons = _load(key)
    cap = snap.get("market_cap_yuan") or (snap.get("market_cap_yi") or 0) * 1e8
    if not cap:
        pytest.skip("该标的快照无市值（港股口径不同）")
    for year, pe in (snap.get("pe_estimates") or {}).items():
        profit = ((cons.get("estimates") or {}).get(year) or {}).get("profit_yuan")
        if not profit or profit <= 0:
            continue
        expected = round(cap / profit, 1)

        assert abs(pe - expected) <= 0.2, (
            f"{key} {year}: 页面 PE {pe} 与同花顺均值口径 {expected} 不符——"
            f"倍数口径被改过？")


def test_strip_does_not_claim_trimming_it_does_not_do():
    """★页面不能说「已剔除极值」——A 之下一个都没剔除。"""
    js = CQ_JS.read_text(encoding="utf-8")
    body = re.sub(r"^\s*//.*$", "", js, flags=re.M)      # 注释里可以复述历史文案

    assert "截尾均值（剔除两端极值后取平均）" not in body
    assert "已在截尾时剔除" not in body


def test_strip_states_the_real_basis():
    """正面断言：必须写清用的是全覆盖算术均值且不剔除极值。"""
    js = CQ_JS.read_text(encoding="utf-8")

    assert "同花顺全覆盖算术均值" in js
    assert "不剔除极值" in js
    assert "页面倍数未剔除" in js


def test_dispersion_shown_for_every_horizon_year():
    """★只显示首年等于把唯一该看的那年藏起来：寒武纪 2026E 区间看着正常（2.6 倍），
    2027E 才是 3.4 倍。区间必须逐年显示。"""
    js = CQ_JS.read_text(encoding="utf-8")
    # 文件里有多个 years.forEach（收集问题的、输出区间的），逐个找而不是只看第一个
    blocks = re.findall(r"years\.forEach\(function \(y\) \{(.{0,1200}?)\n    \}\);", js, re.S)

    assert any("预测区间 ' + y" in b for b in blocks), "预测区间没有按年份逐条输出"


@pytest.mark.parametrize("key", KEYS)
def test_extreme_dispersion_is_visible_somewhere(key):
    """区间极端（最高 ≥ 最低的 2.5 倍）的年份，页面必须有可显示的数据支撑。

    这不是断言页面文案，而是断言**数据里有 min/max 可用**——
    没有的话上面那条逐年显示的逻辑会静默跳过，读者什么都看不到。
    """
    _, cons = _load(key)
    for year in (cons.get("horizon") or []):
        bs = (cons.get("broker_stats") or {}).get(year) or {}
        lo = bs.get("coverage_min") if bs.get("coverage_min") is not None else bs.get("min")
        hi = bs.get("coverage_max") if bs.get("coverage_max") is not None else bs.get("max")
        est = (cons.get("estimates") or {}).get(year) or {}
        if lo is None or hi is None:
            # 用 PS 估值的标的走营收区间，同样要有
            assert est.get("revenue_min_yuan") is not None, f"{key} {year} 无任何可显示的预测区间"
