#!/usr/bin/env python3
"""test_revision_momentum_wiring.py — 预期修正动能接线（地基 8-07 就有，一直没人调用）

## 它回答什么问题

页面只显示「2027E 净利预期 548 亿」这个**当前值**，从不说它是怎么走到今天的。
两家公司页面可以长得一模一样，背后却是相反的故事：一家从 480 亿被一路上调，
另一家从 630 亿被一路下调。修正方向是这套管线里唯一测「市场看法变化率」的维度。

## 2026-08-09 接线前实测到的坑

全池 11 只 × 2 年**全部返回 `flat 0.0%`，跨度 2 天**——当时只有 8-07、8-09
两个观测点，且都是开发时手跑的（cron 是每周一，8-07 周五、8-09 周日），数值一样。
「持平」技术上没错，实质是「隔两天拍了两张照片」，读者却会读成「机构预期稳定」。
与 8-07 否决「从 git 历史刨」（5 个版本全落在两天内）是同一个陷阱。

⚠ 但**不能一刀切按跨度否决**：事件驱动的修正恰恰发生在相邻两周之间，
一刀切会把最想要的信号压掉。故只对「看起来没变」的情形要求跨度。
"""

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "update_research_consensus", _ROOT / "scripts" / "update_research_consensus.py")
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)

Y = urc.horizon_years()[1]          # 次年，随日历滚动，别写死 2027E


def _rows(tmp_path, key, rows):
    """rows: [(as_of, profit_yuan, price_or_None), ...]"""
    p = tmp_path / f"{key}-consensus-history.jsonl"
    lines = []
    for d, v, pr in rows:
        obs = {"as_of": d, "years": {Y: {"profit": v, "revenue": v * 2}}}
        if pr is not None:
            obs["price"] = pr
        lines.append(json.dumps(obs, ensure_ascii=False))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ── 最短跨度守卫 ─────────────────────────────────────────────────────────────


class TestMinSpanGuard:
    def test_two_days_apart_unchanged_is_insufficient(self, tmp_path):
        """★真实生产状态：8-07 和 8-09 两个手跑观测，数值一样。
        没有这道守卫，11 个页面会齐刷刷显示「持平」——读者会当成「机构预期稳定」。"""
        _rows(tmp_path, "300308", [("2026-08-07", 5.481e10, None),
                                   ("2026-08-09", 5.481e10, None)])

        r = urc.revision_momentum("300308", Y, tmp_path)

        assert r["direction"] == "insufficient"

    def test_short_span_but_real_move_is_still_reported(self, tmp_path):
        """★守卫不能一刀切：事件驱动的修正（FCC 调查那类）就发生在相邻两周之间，
        按跨度一律否决会把最想要的信号压掉。"""
        _rows(tmp_path, "300308", [("2026-08-10", 5.0e10, None),
                                   ("2026-08-17", 5.6e10, None)])

        r = urc.revision_momentum("300308", Y, tmp_path)

        assert r["direction"] == "up" and r["change_pct"] == 12.0
        assert r["span_days"] == 7

    def test_long_span_unchanged_is_genuinely_flat(self, tmp_path):
        """跨度够了还没变，那才叫「机构确实没动」。"""
        _rows(tmp_path, "300308", [("2026-08-10", 5.0e10, None),
                                   ("2026-09-14", 5.0e10, None)])

        assert urc.revision_momentum("300308", Y, tmp_path)["direction"] == "flat"

    def test_points_reported_even_when_insufficient(self, tmp_path):
        """页面要显示「已记录 N 次，需约 4 次」，光说「观测不足」读者不知道还差多久。"""
        _rows(tmp_path, "300308", [("2026-08-07", 5.0e10, None),
                                   ("2026-08-09", 5.0e10, None)])

        assert urc.revision_momentum("300308", Y, tmp_path)["points"] == 2

    def test_no_history_file_reports_zero_points(self, tmp_path):
        r = urc.revision_momentum("999999", Y, tmp_path)

        assert r["direction"] == "insufficient" and r["points"] == 0

    def test_points_counted_on_a_real_reading(self, tmp_path):
        _rows(tmp_path, "300308", [("2026-08-10", 5.0e10, None),
                                   ("2026-08-17", 5.2e10, None),
                                   ("2026-08-24", 5.6e10, None)])

        assert urc.revision_momentum("300308", Y, tmp_path)["points"] == 3


# ── 同窗口股价对照 ───────────────────────────────────────────────────────────


class TestPriceComparison:
    def test_same_window_endpoints(self, tmp_path):
        """★错开窗口就没法说「预期涨而价格跌」——那是这个功能唯一的用处。"""
        _rows(tmp_path, "300308", [("2026-08-10", 5.0e10, 1000.0),
                                   ("2026-08-17", 5.6e10, 880.0)])

        r = urc.revision_momentum("300308", Y, tmp_path)

        assert r["change_pct"] == 12.0 and r["price_change_pct"] == -12.0

    def test_missing_price_yields_none_not_zero(self, tmp_path):
        """老观测里没有 price 字段。返回 0% 会被读成「股价没动」。"""
        _rows(tmp_path, "300308", [("2026-08-10", 5.0e10, None),
                                   ("2026-08-17", 5.6e10, 880.0)])

        assert urc.revision_momentum("300308", Y, tmp_path)["price_change_pct"] is None

    def test_price_uses_window_start_not_earliest_record(self, tmp_path):
        """窗口起点由 lookback 截出来，价格必须跟着截，不能取文件里最早那条。"""
        _rows(tmp_path, "300308", [("2026-01-01", 1.0e10, 100.0),
                                   ("2026-08-10", 5.0e10, 1000.0),
                                   ("2026-08-17", 5.6e10, 880.0)])

        r = urc.revision_momentum("300308", Y, tmp_path, lookback_days=90)

        assert r["from_date"] == "2026-08-10" and r["price_change_pct"] == -12.0


# ── 观测里要记价格 ───────────────────────────────────────────────────────────


class TestObservationCarriesPrice:
    _REC = {"symbol": "300308", "fetched_at": "2026-08-10T08:45:00+08:00",
            "estimates": {Y: {"profit_yuan": 5.0e10, "revenue_yuan": 1.0e11}},
            "broker_stats": {Y: {"preferred_value": 5.0e10, "count": 29}}}

    def test_price_written_into_observation(self, tmp_path):
        (tmp_path / "300308-snapshot.json").write_text(json.dumps({"price_yuan": 919.87}))

        urc.append_history("300308", self._REC, tmp_path)
        row = json.loads((tmp_path / "300308-consensus-history.jsonl").read_text().strip())

        assert row["price"] == 919.87

    def test_missing_snapshot_omits_price_field(self, tmp_path):
        """快照缺失时不要写 price:0——0 会被当成一个真实价格算出 ±100%。"""
        urc.append_history("300308", self._REC, tmp_path)
        row = json.loads((tmp_path / "300308-consensus-history.jsonl").read_text().strip())

        assert "price" not in row


# ── 接线本身 ─────────────────────────────────────────────────────────────────


class TestAttachToRecord:
    def test_every_horizon_year_gets_an_entry(self, tmp_path):
        """★算不出也要写字段，否则页面分不清「这只股没数据」和「功能没接上」。"""
        rec = {"estimates": {y: {"profit_yuan": 1e10} for y in urc.horizon_years()}}

        urc.attach_revision_momentum(rec, "300308", tmp_path)

        assert set(rec["revision_momentum"]) == set(urc.horizon_years())
        assert all(v["direction"] == "insufficient" for v in rec["revision_momentum"].values())

    def test_years_beyond_horizon_are_skipped(self, tmp_path):
        """2028E 的修正对「看到 2027 年底就够了」的读者是噪音。"""
        rec = {"estimates": {urc.horizon_years()[0]: {"profit_yuan": 1e10},
                             "2099E": {"profit_yuan": 1e10}}}

        urc.attach_revision_momentum(rec, "300308", tmp_path)

        assert "2099E" not in rec["revision_momentum"]

    def test_history_is_appended_before_momentum_is_computed(self, tmp_path, monkeypatch):
        """★顺序 bug：原实现先写 consensus.json 再 append 历史，
        于是本次观测不在窗口里，页面上的动能永远比数据晚一轮。"""
        monkeypatch.setattr(urc, "DOCS_DATA", tmp_path)
        monkeypatch.setattr(urc, "DEPLOY_DATA", tmp_path / "nope")
        (tmp_path / "300308-snapshot.json").write_text(json.dumps({"price_yuan": 880.0}))
        (tmp_path / "300308-consensus-history.jsonl").write_text(json.dumps(
            {"as_of": "2026-08-10", "years": {Y: {"profit": 5.0e10}}, "price": 1000.0}) + "\n")
        rec = {"symbol": "300308", "fetched_at": "2026-08-17T08:45:00+08:00",
               "estimates": {Y: {"profit_yuan": 5.6e10}},
               "broker_stats": {Y: {"preferred_value": 5.6e10, "count": 29}}}

        urc.write_and_deploy("300308", rec)
        written = json.loads((tmp_path / "300308-consensus.json").read_text())

        m = written["revision_momentum"][Y]
        assert m["to_date"] == "2026-08-17", "今天的观测没进窗口 → 动能晚一轮"
        assert m["change_pct"] == 12.0 and m["price_change_pct"] == -12.0
