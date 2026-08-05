#!/usr/bin/env python3
"""test_consensus_hk_et_brokers.py — 港股逐家机构明细（经济通 ETNet）

**范围是刻意收窄的**（2026-08-04 用户复核后修订）：

港股的一致预期验证只有**一层**能做——同一源内的多机构对比。
跨源验证做不了，因为 yfinance 给营收+EPS、ET 给净利，**没有任何一个指标两源都有**；
要打通就得除以股本，而股本是不确定量，会制造假的 CONFIRMED/DIVERGENT。

原计划里的两项已删除，理由记在这里以免以后又想加回去：
- **目标价交叉**：目标价 = 盈利预测 × 假设倍数，是复合量。两人可以盈利预测相同
  而倍数差一倍。且 ET 与 yfinance 底层聚合的券商报告本就重叠，一致有一部分
  来自共享输入，不构成独立确认。
- **评级交叉**：序数、压缩成三五档、严重偏向买入（旭创 29 家 25 买入 0 卖出），
  几乎没有信息量。

单位已由 ET「去年度业绩表现」直接证实：`集团纯利 -4,698.20 百万元人民币`
→ `纯利/亏损` 单位 = **百万元**。`每股盈利`（单位「分」）不用——各家对未来股本
（增发/摊薄）假设不同，数值不可比；这是口径差异而非脏数据。
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_consensus.py"
_spec = importlib.util.spec_from_file_location("update_research_consensus", _SCRIPT)
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)

_FIX = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "et_consensus" / "02513.json").read_text()
)
TODAY = date(2026, 8, 4)


# ── 解析逐家明细 ──────────────────────────────────────────────────────────────


def test_parse_et_brokers_groups_by_fiscal_year():
    out = urc.parse_et_brokers(_FIX["columns"], _FIX["rows"])

    assert sorted(out) == ["2026E", "2027E", "2028E"]


def test_parse_et_brokers_converts_millions_to_yuan():
    """`纯利/亏损` 单位是百万元 —— 不换算会让港股与 A 股差 6 个数量级。"""
    out = urc.parse_et_brokers(_FIX["columns"], _FIX["rows"])
    vals = [r["value"] for r in out["2026E"]]

    assert min(vals) == pytest.approx(-5_504_000_000.0)   # 瑞银 -5504 百万
    assert max(vals) == pytest.approx(-3_605_000_000.0)   # 摩根大通 -3605 百万


def test_parse_et_brokers_keeps_org_and_publish_date():
    """新鲜度加权与离群溯源都依赖这两个字段。"""
    out = urc.parse_et_brokers(_FIX["columns"], _FIX["rows"])
    rec = next(r for r in out["2026E"] if r["org"] == "海通")

    assert rec["published"] == "2026-07-20"
    assert rec["value"] == pytest.approx(-4_312_000_000.0)


def test_parse_et_brokers_has_thirteen_brokers_for_2026():
    assert len(urc.parse_et_brokers(_FIX["columns"], _FIX["rows"])["2026E"]) == 13


def test_parse_et_brokers_drops_rows_without_profit():
    rows = [["2026", "", "-1030.0", "", "某券商", "买入", "1500.0", "2026-07-01"]]

    assert urc.parse_et_brokers(_FIX["columns"], rows) == {}


def test_parse_et_brokers_ignores_eps_column():
    """`每股盈利` 各家股本假设不同、不可比，必须不出现在结果里。"""
    out = urc.parse_et_brokers(_FIX["columns"], _FIX["rows"])

    for recs in out.values():
        for r in recs:
            assert set(r) == {"org", "published", "value"}


def test_parse_et_brokers_handles_missing_columns():
    assert urc.parse_et_brokers(["foo", "bar"], [["a", "b"]]) == {}


# ── 与既有稳健统计层对接 ──────────────────────────────────────────────────────


def test_et_brokers_feed_robust_stats_median():
    """ET「综合盈利预测」给的 2026 综合值 -4312 百万经验证正是中位数，
    我们自算的中位数必须与之吻合 —— 这是对解析正确性的独立校验。"""
    per_year = urc.parse_et_brokers(_FIX["columns"], _FIX["rows"])

    stats = urc.robust_stats(per_year, today=TODAY)["2026E"]

    assert stats["median"] == pytest.approx(-4_312_000_000.0)
    assert stats["count"] == 13
    # 采用口径 2026-08-05 起为截尾均值；中位数仍是校验解析正确性的锚
    assert stats["preferred_stat"] == "trimmed_mean"


def test_et_brokers_recency_weighting_applies():
    per_year = urc.parse_et_brokers(_FIX["columns"], _FIX["rows"])

    stats = urc.robust_stats(per_year, today=TODAY)["2026E"]

    assert stats["oldest_age_days"] is not None
    assert stats["newest_age_days"] is not None
    assert stats["oldest_age_days"] >= stats["newest_age_days"]


def test_et_brokers_outliers_use_dual_gate():
    """13 家净利集中在 -3605~-5504（极差仅 1.53 倍），双门槛下不应有离群。"""
    per_year = urc.parse_et_brokers(_FIX["columns"], _FIX["rows"])

    stats = urc.robust_stats(per_year, today=TODAY)["2026E"]

    assert stats["outliers"] == []
