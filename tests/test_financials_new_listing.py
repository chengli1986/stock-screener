#!/usr/bin/env python3
"""test_financials_new_listing.py — 次新股无十大流通股东披露时的降级

触发问题(2026-08-03 实测):长鑫科技 688825 于 2026-07-27 上市,首份定期报告(三季报)
要到 10 月才发布,`ak.stock_circulate_stock_holder("688825")` 返回空表并抛
`KeyError: "None of [Index(['截止日期', ...])] are in the [columns]"`。

原实现直接用 `df_sh["截止日期"].max()`,该异常会让整只股票 FAILED、
`update_research_financials.py` exit 1,进而让月度 cron 告警——而这只是「新股还没到
披露时点」这一完全正常的状态。

同时:股东户数(东财 RPT_F10_EH_HOLDERNUM)对新股**是有数据的**
(688825 上市日 2026-07-27 已披露 3,456,664 户),不能因为十大股东缺失就一起丢掉。
"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_financials.py"
_spec = importlib.util.spec_from_file_location("update_research_financials", _SCRIPT)
urf = importlib.util.module_from_spec(_spec)
sys.modules["update_research_financials"] = urf
_spec.loader.exec_module(urf)


_COLS = ["截止日期", "公告日期", "编号", "股东名称", "持股数量", "占流通股比例", "股本性质"]


def _holder_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["2026-03-31", "2026-04-20", 1, "清辉集电", 1000000, 21.67, "流通A股"],
            ["2026-03-31", "2026-04-20", 2, "长鑫集成", 500000, 11.71, "流通A股"],
            ["2025-12-31", "2026-01-20", 1, "清辉集电", 900000, 20.10, "流通A股"],
        ],
        columns=_COLS,
    )


# ── parse_top10:正常披露 ──────────────────────────────────────────────────────


def test_parse_top10_takes_latest_period_only():
    """只取最新截止日期那一期,不能把历史各期混在一起。"""
    top10 = urf.parse_top10(_holder_df())

    assert [h["name"] for h in top10] == ["清辉集电", "长鑫集成"]
    assert top10[0]["pct"] == pytest.approx(21.67)
    assert top10[0]["shares"] == 1000000
    assert top10[0]["rank"] == 1


def test_parse_top10_caps_at_ten_rows():
    df = pd.DataFrame(
        [["2026-03-31", "2026-04-20", i + 1, f"股东{i}", 100, 1.0, "流通A股"] for i in range(15)],
        columns=_COLS,
    )

    assert len(urf.parse_top10(df)) == 10


# ── parse_top10:次新股尚未披露 ────────────────────────────────────────────────


def test_parse_top10_returns_empty_for_empty_frame():
    """空表 = 还没到披露时点,不是错误。"""
    assert urf.parse_top10(pd.DataFrame()) == []


def test_parse_top10_returns_empty_when_expected_columns_missing():
    """akshare 对无数据的新股返回列名不符的空表 —— 不能抛 KeyError。"""
    assert urf.parse_top10(pd.DataFrame({"foo": [1, 2]})) == []


def test_parse_top10_returns_empty_for_none():
    assert urf.parse_top10(None) == []


# ── fetch_shareholders:十大股东失败不应带走股东户数 ───────────────────────────


def test_shareholder_fetch_keeps_holder_count_when_top10_unavailable(monkeypatch):
    """688825 现状:股东户数有(345.67 万户),十大流通股东无。

    这条测试的意义:如果实现把两者写在同一个 try 里,户数会被一起丢掉,
    而户数恰恰是次新股唯一能拿到的持仓信息。
    """
    import unittest.mock as mock

    fake_json = {
        "success": True,
        "result": {"data": [{
            "END_DATE": "2026-07-27 00:00:00",
            "HOLDER_TOTAL_NUM": 3456664,
            "TOTAL_NUM_RATIO": 5761006.6667,
        }]},
    }
    resp = mock.Mock()
    resp.json.return_value = fake_json
    resp.raise_for_status.return_value = None

    def _boom(*a, **kw):
        raise KeyError("None of [Index([...])] are in the [columns]")

    monkeypatch.setattr(urf, "_get_with_retry", lambda *a, **kw: resp, raising=False)
    monkeypatch.setattr(urf.ak, "stock_circulate_stock_holder", _boom)

    out = urf.fetch_shareholders("688825", "SH")

    assert out["total_count"] == 3456664
    assert out["report_date"] == "2026-07-27"
    assert out["top10"] == []
