#!/usr/bin/env python3
"""test_snapshot_consensus_source.py — 快照的估值分母改读自动抓取的 consensus.json

**触发问题（2026-08-04 审计发现）**：一致预期抓取（`update_research_consensus.py`）
的产物落在 `docs-site/data/{key}-consensus.json`，但 `update_research_snapshots.py`
计算 PE/PS 用的仍是 `config/research_stocks.json` 里的**注册表冻结值**，
而 11 个研报页读的又是快照 → **页面显示的估值倍数一直是化石**。

实测偏差（18 项 >5%，方向不一致故不能当作「整体保守」）：
- 寒武纪 2027E PE：页面 90.3 vs 应为 59.3（**高估 52%**）
- 源杰 2027E PS：页面 46.8 vs 应为 61.5（**低估 24%**）
- 中际旭创 2027E PE：24.9 vs 21.8

同时观察池卡片已接 consensus.json 是对的 —— 于是同一网站两处给出不同的 PE，
比单纯过时更糟。

**修法（用户选定方案 A）**：快照改以 consensus.json 为准，注册表降为兜底。
注册表当「人工权威口径」的原意是「自动抓取需要人工把关」，
但把关已由双源交叉复核 + 中位数 + 离群标记 + 背离告警自动化，
再留一道人工闸门只会制造化石。

## 后续：兜底整个删掉（2026-08-09，用户拍板）

降为兜底之后问题只解决了一半：**一次失败的抓取既不报错也不留白，而是平静地
渲染出一个用登记日旧预期算出的错 PE**（旭创注册表记 480 亿 vs 实际 548 亿，差 14%）。
在「读研报下投资判断」的用法下，静默的错数字比明显的空值危险得多。
用户拍板：**宁可不显示，也不显示错的**。失败即 `source="missing"`、分母为空、
页面不渲染倍数，由 data-health 告警。本文件下半部分即这条新契约的定义。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_snapshots.py"
_spec = importlib.util.spec_from_file_location("update_research_snapshots", _SCRIPT)
urs = importlib.util.module_from_spec(_spec)
sys.modules["update_research_snapshots"] = urs
_spec.loader.exec_module(urs)


# 2026-08-09 起注册表不再带 consensus 块——留着也不会被读，留着反而会让人
# 以为兜底还在。这里保留一个**带冻结值的**副本，专门用于断言「即使有，也不许用」。
_STOCK = {
    "symbol": "688256", "exchange": "SH", "name": "寒武纪", "snapshot_key": "688256",
    "valuation_mode": "pe",
}
_STOCK_WITH_STALE_REGISTRY = dict(_STOCK, consensus={
    "2026E": {"label": "2026E", "profit_yuan": 4_500_000_000},
    "2027E": {"label": "2027E", "profit_yuan": 7_500_000_000},
})

_AUTO_CONSENSUS = {
    "symbol": "688256", "name": "寒武纪", "source": "ths",
    "estimates": {
        "2026E": {"profit_yuan": 5_480_000_000, "orgs": 15},
        "2027E": {"profit_yuan": 11_420_000_000, "orgs": 14},
        "2028E": {"profit_yuan": 18_340_000_000, "orgs": 13},
    },
}


def _write(tmp_path: Path, payload: dict, key: str = "688256") -> Path:
    f = tmp_path / f"{key}-consensus.json"
    f.write_text(json.dumps(payload, ensure_ascii=False))
    return f


# ── load_consensus_estimates ─────────────────────────────────────────────────


def test_loads_estimates_from_consensus_file(tmp_path):
    _write(tmp_path, _AUTO_CONSENSUS)

    est = urs.load_consensus_estimates("688256", data_dir=tmp_path)

    assert est["2027E"]["profit_yuan"] == 11_420_000_000


def test_returns_empty_when_file_absent(tmp_path):
    assert urs.load_consensus_estimates("999999", data_dir=tmp_path) == {}


def test_returns_empty_on_corrupt_file(tmp_path):
    (tmp_path / "688256-consensus.json").write_text("{ not json")

    assert urs.load_consensus_estimates("688256", data_dir=tmp_path) == {}


# ── resolve_consensus：只认自动源，失败即 missing ────────────────────────────


def test_uses_auto_consensus(tmp_path):
    _write(tmp_path, _AUTO_CONSENSUS)

    est, source = urs.resolve_consensus(_STOCK, data_dir=tmp_path)

    assert source == "auto"
    assert est["2027E"]["profit_yuan"] == 11_420_000_000


def test_auto_consensus_brings_extra_forecast_years(tmp_path):
    """自动源多给的 2028E 应保留 —— 注册表只有两年。"""
    _write(tmp_path, _AUTO_CONSENSUS)

    est, _ = urs.resolve_consensus(_STOCK, data_dir=tmp_path)

    assert "2028E" in est


def test_missing_auto_file_yields_missing_not_registry(tmp_path):
    """★核心契约：抓不到就是抓不到，不许拿注册表冻结值顶上。"""
    est, source = urs.resolve_consensus(_STOCK, data_dir=tmp_path)

    assert source == "missing"
    assert est == {}


def test_stale_registry_block_is_ignored_even_if_present(tmp_path):
    """★即便注册表里还留着冻结值（旧配置没清干净），也绝不能用。"""
    est, source = urs.resolve_consensus(_STOCK_WITH_STALE_REGISTRY, data_dir=tmp_path)

    assert source == "missing"
    assert est == {}


def test_empty_auto_file_yields_missing(tmp_path):
    """抓取失败留下的空壳文件 —— 分母为空，页面不渲染倍数。"""
    _write(tmp_path, {"symbol": "688256", "estimates": {}})

    est, source = urs.resolve_consensus(_STOCK_WITH_STALE_REGISTRY, data_dir=tmp_path)

    assert source == "missing"
    assert est == {}


def test_stock_without_any_consensus_yields_missing(tmp_path):
    est, source = urs.resolve_consensus({"snapshot_key": "x"}, data_dir=tmp_path)

    assert est == {} and source == "missing"


# ── build_snapshot 集成 ──────────────────────────────────────────────────────


class TestSnapshotUsesAutoConsensus:
    _QUOTE = {"price_yuan": 1080.0, "market_cap_yuan": 678_600_000_000.0,
              "change_pct": 5.1, "vol_wan_shou": 20.0}
    _OHLCV = {"year_return_pct": 127.3, "period_return_pct": 127.3, "history_days": 241,
              "ma20": 1298.0, "ma20_slope": "down", "vol_60d_ann_pct": 82.5,
              "vol_ratio_5_60": 0.9, "week52_high": 1620.0, "week52_low": 448.66,
              "week52_is_full": True}

    def _build(self, tmp_path):
        import unittest.mock as mock
        with mock.patch.object(urs, "fetch_quote_data", return_value=self._QUOTE), \
             mock.patch.object(urs, "fetch_ohlcv_data", return_value=self._OHLCV), \
             mock.patch.object(urs, "DATA_DIR", tmp_path):
            return urs.build_snapshot(_STOCK_WITH_STALE_REGISTRY)

    def test_pe_computed_from_auto_consensus(self, tmp_path):
        """6786 亿 / 114.2 亿 = 59.4x（而非用冻结值算出的 90.5x）。"""
        _write(tmp_path, _AUTO_CONSENSUS)

        snap = self._build(tmp_path)

        assert snap["pe_estimates"]["2027E"] == pytest.approx(59.4, abs=0.3)

    def test_snapshot_records_which_consensus_was_used(self, tmp_path):
        """页面要能显示这个 PE 是用哪一版预期算的，否则下次分叉还是发现不了。"""
        _write(tmp_path, _AUTO_CONSENSUS)

        assert self._build(tmp_path)["consensus_source"] == "auto"

    def test_snapshot_marks_missing_when_fetch_failed(self, tmp_path):
        assert self._build(tmp_path)["consensus_source"] == "missing"

    def test_no_multiples_rendered_when_consensus_missing(self, tmp_path):
        """★改动前这里会渲染出 90.5x（用注册表冻结值算的）。现在必须留空——
        页面宁可缺一块，也不能显示一个看起来很正常的错数字。"""
        snap = self._build(tmp_path)

        assert snap["pe_estimates"] == {}


# ── 只对预测年份算估值倍数 ────────────────────────────────────────────────────
#
# 自动源的 estimates 同时含实际年份（`2023A`/`2024A`/`2025A`，来自同花顺详细指标表的
# 「实际值」列）与预测年份（`2026E`…）。**用今天的市值除以三年前的利润没有意义**
# —— 首次接入方案 A 时把全部年份都算了，产出 "2023A PE 549.9" 这种数。


def test_resolve_consensus_drops_actual_years(tmp_path):
    _write(tmp_path, {
        "symbol": "688256",
        "estimates": {
            "2024A": {"profit_yuan": 1_000_000_000},
            "2025A": {"profit_yuan": 2_000_000_000},
            "2026E": {"profit_yuan": 5_480_000_000},
            "2027E": {"profit_yuan": 11_420_000_000},
        },
    })

    est, source = urs.resolve_consensus(_STOCK, data_dir=tmp_path)

    assert source == "auto"
    assert sorted(est) == ["2026E", "2027E"], "实际年份未被剔除"


def test_only_actual_years_counts_as_missing(tmp_path):
    """只有实际值、没有任何预测 —— 等同于没抓到。"""
    _write(tmp_path, {"symbol": "688256", "estimates": {"2025A": {"profit_yuan": 2e9}}})

    est, source = urs.resolve_consensus(_STOCK_WITH_STALE_REGISTRY, data_dir=tmp_path)

    assert source == "missing" and est == {}


def test_snapshot_has_no_actual_year_multiples(tmp_path):
    import unittest.mock as mock
    _write(tmp_path, {
        "symbol": "688256",
        "estimates": {
            "2025A": {"profit_yuan": 2_000_000_000},
            "2026E": {"profit_yuan": 5_480_000_000},
        },
    })
    q = {"price_yuan": 1080.0, "market_cap_yuan": 678_600_000_000.0,
         "change_pct": 5.1, "vol_wan_shou": 20.0}
    o = {"year_return_pct": 127.3, "period_return_pct": 127.3, "history_days": 241,
         "ma20": 1298.0, "ma20_slope": "down", "vol_60d_ann_pct": 82.5,
         "vol_ratio_5_60": 0.9, "week52_high": 1620.0, "week52_low": 448.66,
         "week52_is_full": True}
    with mock.patch.object(urs, "fetch_quote_data", return_value=q), \
         mock.patch.object(urs, "fetch_ohlcv_data", return_value=o), \
         mock.patch.object(urs, "DATA_DIR", tmp_path):
        snap = urs.build_snapshot(_STOCK_WITH_STALE_REGISTRY)

    assert list(snap["pe_estimates"]) == ["2026E"]
