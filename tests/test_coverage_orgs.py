#!/usr/bin/env python3
"""test_coverage_orgs.py — 覆盖机构数用错了源（用户 2026-08-09 质疑带出的 bug）

## 用户的质疑

页面显示「长鑫仅 2 家机构覆盖」。用户直觉不对：3.5 万亿市值的 DRAM 龙头、
世界第一梯队，不可能只有 2 家券商跟。

## 实测确认：全池系统性低估

| 标的 | 同花顺覆盖机构数 | 东财逐家明细条数 | 页面显示 |
|---|---|---|---|
| 贵州茅台 | **46** | 31 | 31 ❌ |
| 宁德时代 | **39** | 27 | 27 ❌ |
| 长鑫科技 | **4** | 2 | 2 ❌ |

两个数字含义不同：
- `estimates[year].orgs`（同花顺汇总）＝**有多少家机构给了该年预测**
- `broker_stats[year].count`（东财 F10 明细）＝**能拿到逐家姓名与数值的有多少条**

后者永远 ≤ 前者。`robust_stats()` 用后者是对的——中位数、MAD 离群、
新鲜度加权都必须有逐家数据。**错在把它当「覆盖机构数」展示**。

预测区间同理：长鑫同花顺 1,244~1,518 亿（4 家），东财明细只有 1,449~1,518（2 家），
少了一家更悲观的。茅台宁德恰好一致，是因为东财那 31/27 家里已含极值机构——
**这种「恰好对上」最危险，它让 bug 在大盘股上完全隐形**。

## 为什么连薄覆盖判断也要改

当前 `insufficient_samples = count < 5`。实测三只薄覆盖标的（长光 3/3、风华 3/3、
长鑫 4/2）**结论恰好都不变**，但逻辑仍是错的：一旦出现 orgs=8 而 count=4，
就会把覆盖充分的标的误判为样本不足，并连带触发买点告警的可信度门禁。
不能因为「现在恰好没错」就留着。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_consensus.py"
_spec = importlib.util.spec_from_file_location("update_research_consensus", _SCRIPT)
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)


def _record(orgs=46, count=31, lo=8.13e10, hi=9.75e10):
    return {
        "estimates": {"2026E": {"profit_yuan": 8.6e10, "orgs": orgs,
                                "profit_min_yuan": lo, "profit_max_yuan": hi}},
        "broker_stats": {"2026E": {"count": count, "min": 8.13e10, "max": 9.75e10,
                                   "insufficient_samples": count < 5}},
    }


class TestMergeCoverageOrgs:
    def test_coverage_orgs_taken_from_ths(self):
        """茅台：同花顺 46 家才是覆盖数，东财 31 条只是有明细的。"""
        r = urc.merge_coverage_orgs(_record(orgs=46, count=31))

        assert r["broker_stats"]["2026E"]["coverage_orgs"] == 46

    def test_detail_count_is_preserved_separately(self):
        """明细条数不能丢——离散度统计是基于它算的，读者要知道样本量。"""
        r = urc.merge_coverage_orgs(_record(orgs=46, count=31))

        assert r["broker_stats"]["2026E"]["count"] == 31

    def test_thin_coverage_judged_by_coverage_not_detail(self):
        """★核心：orgs=8 覆盖充分，但只有 4 条明细——不该判样本不足。
        当前实测数据里没有这种组合，但逻辑必须对，否则一旦出现就会误判，
        并连带触发买点告警的可信度门禁。"""
        r = urc.merge_coverage_orgs(_record(orgs=8, count=4))

        assert r["broker_stats"]["2026E"]["insufficient_samples"] is False

    def test_genuinely_thin_still_flagged(self):
        """长鑫：覆盖 4 家（<5），仍然是真薄覆盖。"""
        r = urc.merge_coverage_orgs(_record(orgs=4, count=2))

        assert r["broker_stats"]["2026E"]["insufficient_samples"] is True

    def test_falls_back_to_count_when_orgs_absent(self):
        """智谱走 yfinance/ETNet，没有同花顺的 orgs —— 退回明细条数，不能崩。"""
        rec = _record(orgs=None, count=13)
        del rec["estimates"]["2026E"]["orgs"]

        r = urc.merge_coverage_orgs(rec)

        assert r["broker_stats"]["2026E"]["coverage_orgs"] == 13
        assert r["broker_stats"]["2026E"]["insufficient_samples"] is False

    def test_wider_range_from_ths_is_carried(self):
        """★长鑫实测：同花顺 1,244~1,518 亿（4 家），东财明细只有 1,449~1,518（2 家）。
        少的那家更悲观——展示窄区间会让读者以为分歧比实际小。"""
        r = urc.merge_coverage_orgs(_record(orgs=4, count=2, lo=1.24405e11, hi=1.51831e11))
        bs = r["broker_stats"]["2026E"]

        assert bs["coverage_min"] == pytest.approx(1.24405e11)
        assert bs["coverage_max"] == pytest.approx(1.51831e11)

    def test_detail_range_kept_for_comparison(self):
        """两个区间都留着——差异本身是信息（说明明细缺了多少）。"""
        r = urc.merge_coverage_orgs(_record(orgs=4, count=2, lo=1.24405e11, hi=1.51831e11))
        bs = r["broker_stats"]["2026E"]

        assert bs["min"] == pytest.approx(8.13e10)   # 原东财明细值原样保留

    def test_years_without_stats_are_untouched(self):
        rec = {"estimates": {"2028E": {"orgs": 5}}, "broker_stats": {}}

        r = urc.merge_coverage_orgs(rec)

        assert r["broker_stats"] == {}

    def test_missing_broker_stats_does_not_crash(self):
        assert urc.merge_coverage_orgs({"estimates": {}}) is not None


class TestWiredIntoBuildRecord:
    """算了没接 = 白算——这套管线已经犯过两次（preferred_stat、tencent 码）。"""

    def test_build_record_includes_coverage_orgs(self):
        parsed = {"2026E": {"revenue": 1.0e11, "profit": 8.6e10}}
        dispersion = {"2026": {"orgs": 46, "min": 8.13e10, "max": 9.75e10, "spread_ratio": 1.2}}
        rec = urc.build_record({"symbol": "600519", "name": "贵州茅台"},
                               parsed, dispersion, "2026-08-09T08:45:00+08:00")
        rec["broker_stats"] = {"2026E": {"count": 31, "insufficient_samples": False}}
        rec = urc.merge_coverage_orgs(rec)

        assert rec["broker_stats"]["2026E"]["coverage_orgs"] == 46


def test_write_and_deploy_merges_coverage(tmp_path, monkeypatch):
    """接在落盘入口，两条赋值路径自动都覆盖到。"""
    import json as _json
    monkeypatch.setattr(urc, "DOCS_DATA", tmp_path)
    monkeypatch.setattr(urc, "DEPLOY_DATA", tmp_path / "nope")

    rec = {"symbol": "600519", "name": "贵州茅台",
           "fetched_at": "2026-08-09T08:45:00+08:00",
           "estimates": {"2026E": {"profit_yuan": 8.6e10, "orgs": 46,
                                   "profit_min_yuan": 8.13e10, "profit_max_yuan": 9.75e10}},
           "broker_stats": {"2026E": {"count": 31, "insufficient_samples": False}}}
    urc.write_and_deploy("600519", rec)

    on_disk = _json.loads((tmp_path / "600519-consensus.json").read_text())
    assert on_disk["broker_stats"]["2026E"]["coverage_orgs"] == 46
    assert on_disk["broker_stats"]["2026E"]["count"] == 31
