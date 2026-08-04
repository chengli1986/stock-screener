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


_REGISTRY_STOCK = {
    "symbol": "688256", "exchange": "SH", "name": "寒武纪", "snapshot_key": "688256",
    "valuation_mode": "pe",
    "consensus": {   # 冻结值：2026-05-06 注册以来未动
        "2026E": {"label": "2026E", "profit_yuan": 4_500_000_000},
        "2027E": {"label": "2027E", "profit_yuan": 7_500_000_000},
    },
}

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


# ── resolve_consensus：自动优先，注册表兜底 ──────────────────────────────────


def test_prefers_auto_consensus_over_registry(tmp_path):
    _write(tmp_path, _AUTO_CONSENSUS)

    est, source = urs.resolve_consensus(_REGISTRY_STOCK, data_dir=tmp_path)

    assert source == "auto"
    assert est["2027E"]["profit_yuan"] == 11_420_000_000, "仍在用注册表冻结值"


def test_auto_consensus_brings_extra_forecast_years(tmp_path):
    """自动源多给的 2028E 应保留 —— 注册表只有两年。"""
    _write(tmp_path, _AUTO_CONSENSUS)

    est, _ = urs.resolve_consensus(_REGISTRY_STOCK, data_dir=tmp_path)

    assert "2028E" in est


def test_falls_back_to_registry_when_no_auto_file(tmp_path):
    est, source = urs.resolve_consensus(_REGISTRY_STOCK, data_dir=tmp_path)

    assert source == "registry"
    assert est["2027E"]["profit_yuan"] == 7_500_000_000


def test_falls_back_when_auto_file_has_no_estimates(tmp_path):
    """抓取失败留下的空壳文件不能把估值分母清空。"""
    _write(tmp_path, {"symbol": "688256", "estimates": {}})

    est, source = urs.resolve_consensus(_REGISTRY_STOCK, data_dir=tmp_path)

    assert source == "registry"
    assert est["2026E"]["profit_yuan"] == 4_500_000_000


def test_registry_without_consensus_yields_empty(tmp_path):
    est, source = urs.resolve_consensus({"snapshot_key": "x"}, data_dir=tmp_path)

    assert est == {} and source == "registry"


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
            return urs.build_snapshot(_REGISTRY_STOCK)

    def test_pe_computed_from_auto_consensus(self, tmp_path):
        """6786 亿 / 114.2 亿 = 59.4x（而非用冻结值算出的 90.5x）。"""
        _write(tmp_path, _AUTO_CONSENSUS)

        snap = self._build(tmp_path)

        assert snap["pe_estimates"]["2027E"] == pytest.approx(59.4, abs=0.3)

    def test_snapshot_records_which_consensus_was_used(self, tmp_path):
        """页面要能显示这个 PE 是用哪一版预期算的，否则下次分叉还是发现不了。"""
        _write(tmp_path, _AUTO_CONSENSUS)

        assert self._build(tmp_path)["consensus_source"] == "auto"

    def test_snapshot_marks_registry_fallback(self, tmp_path):
        assert self._build(tmp_path)["consensus_source"] == "registry"

    def test_registry_fallback_keeps_old_behaviour(self, tmp_path):
        """兜底路径必须与改动前逐位一致：6786 亿 / 75 亿 = 90.5x。"""
        snap = self._build(tmp_path)

        assert snap["pe_estimates"]["2027E"] == pytest.approx(90.5, abs=0.3)


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

    est, source = urs.resolve_consensus(_REGISTRY_STOCK, data_dir=tmp_path)

    assert source == "auto"
    assert sorted(est) == ["2026E", "2027E"], "实际年份未被剔除"


def test_resolve_consensus_falls_back_when_only_actual_years(tmp_path):
    """只有实际值、没有任何预测 —— 等同于没抓到，应回落注册表。"""
    _write(tmp_path, {"symbol": "688256", "estimates": {"2025A": {"profit_yuan": 2e9}}})

    est, source = urs.resolve_consensus(_REGISTRY_STOCK, data_dir=tmp_path)

    assert source == "registry"


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
        snap = urs.build_snapshot(_REGISTRY_STOCK)

    assert list(snap["pe_estimates"]) == ["2026E"]
