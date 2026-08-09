#!/usr/bin/env python3
"""test_thesis_drift.py — 研报正文的估值结论被现价推翻时要能发现（未决问题 ③）

## 2026-08-09 实测到的两处「结论已反」

- 旭创正文：「当前 ~44x 已高于乐观情景上沿（35x）」→ 实际 2026E PE 34.9x，
  **落回 30–35 档内，已不高于乐观上沿**。
- 寒武纪正文：「当前 ~173x 高于三情景目标区间（100/120/150）」→ 实际 137.7x，
  **落回 120–150 档内**。

两页的投资判断都在讲一件不再成立的事，而 `verify_report_js.py` 只查 id/字段一致性，
查不出这种「数字过时」。narrative 周更 cron 于 2026-08-07 暂停后更无人把关。

## 两条刻意的设计约束

1. **不扫正文全部数字**。11 页共 552 处「N倍/Nx」，多数是产能倍数、同业倍数、
   情景目标、带日期的历史锚点，都不该被订正。改为登记表逐股手工确认。
2. **只在状态变化时告警**。「结论已反」是持续状态，天天报＝自我废弃
   （与 test_divergence_alert_transitions.py 同一教训）。
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "research_thesis_drift", _ROOT / "scripts" / "research_thesis_drift.py")
td = importlib.util.module_from_spec(_spec)
sys.modules["research_thesis_drift"] = td
_spec.loader.exec_module(td)

INNOLIGHT = {"name": "中际旭创", "page": "/innolight-300308.html", "metric": "pe",
             "year": "2026E", "bands": [25, 30, 35],
             "band_labels": ["保守 25x", "基准 30x", "乐观 35x"], "asserted": 44.2,
             "claim": "当前 ~44x 已高于乐观情景上沿（35x）"}


def _snap(pe=None, ps=None):
    return {"pe_estimates": pe or {}, "ps_estimates": ps or {}}


# ── 落在第几档 ───────────────────────────────────────────────────────────────


class TestZone:
    def test_below_all(self):
        assert td.zone(20, [25, 30, 35]) == 0

    def test_between(self):
        assert td.zone(32, [25, 30, 35]) == 2

    def test_above_all(self):
        assert td.zone(44, [25, 30, 35]) == 3

    def test_on_the_boundary_counts_as_below(self):
        """恰好等于档位值不算「高于」——旭创 34.9 vs 乐观 35 就卡在这个边上。"""
        assert td.zone(35, [25, 30, 35]) == 2

    def test_descending_ps_scenarios_work_too(self):
        """PS 情景表方向是反的（营收越低 → forward PS 越高）。
        判定只用序号，不解析档位名，所以不必关心哪头是保守。"""
        assert td.zone(120, [74, 106, 149]) == 2
        assert td.zone(60, [74, 106, 149]) == 0


# ── 结论翻转 ─────────────────────────────────────────────────────────────────


class TestVerdictFlip:
    def test_innolight_conclusion_is_now_false(self):
        """★真实案例：正文说「高于乐观上沿 35x」，实际 34.9x 已落回档内。"""
        r = td.evaluate("300308", INNOLIGHT, _snap(pe={"2026E": 34.9}), 20)

        assert r is not None
        assert "推翻" in r["what"]
        assert "34.9" in r["detail"] and "44.2" in r["detail"]

    def test_cambricon_fell_back_into_the_band(self):
        e = {"name": "寒武纪", "page": "/cambricon-688256.html", "metric": "pe",
             "year": "2026E", "bands": [100, 120, 150], "asserted": 173.0, "claim": "x"}
        r = td.evaluate("688256", e, _snap(pe={"2026E": 137.7}), 20)

        assert r is not None and r["status"] == "zone:2"

    def test_same_zone_is_not_reported(self):
        """价格动了但没跨档 → 结论仍然成立，不该打扰。"""
        assert td.evaluate("300308", INNOLIGHT, _snap(pe={"2026E": 41.0}), 20) is None

    def test_still_above_all_bands_is_silent(self):
        e = dict(INNOLIGHT, asserted=90.0, bands=[30, 35, 40], metric="ps")
        assert td.evaluate("688702", e, _snap(ps={"2026E": 95.3}), 20) is None

    def test_claim_is_carried_into_the_finding(self):
        """告警要能直接定位改哪一句，否则读者只知道「有问题」。"""
        r = td.evaluate("300308", INNOLIGHT, _snap(pe={"2026E": 34.9}), 20)

        assert r["claim"] == INNOLIGHT["claim"]


# ── 没有三档时退回相对偏离 ───────────────────────────────────────────────────


class TestDriftFallback:
    _E = {"name": "贵州茅台", "page": "/moutai-600519.html", "metric": "pe",
          "year": "2026E", "bands": None, "asserted": 17.5, "claim": "y"}

    def test_small_drift_silent(self):
        assert td.evaluate("600519", self._E, _snap(pe={"2026E": 19.0}), 20) is None

    def test_large_drift_reported(self):
        r = td.evaluate("02513", dict(self._E, asserted=528.0, metric="ps"),
                        _snap(ps={"2026E": 129.7}), 20)

        assert r is not None and r["status"] == "drift:low"

    def test_drift_direction_recorded(self):
        r = td.evaluate("600519", dict(self._E, asserted=10.0),
                        _snap(pe={"2026E": 19.0}), 20)

        assert r["status"] == "drift:high"


# ── 配置漂移也要报 ───────────────────────────────────────────────────────────


class TestConfigDrift:
    def test_year_missing_from_snapshot_is_reported(self):
        """★视界滚动后登记表若不跟上，静默不报就等于这只股永远不被检查。"""
        r = td.evaluate("300308", dict(INNOLIGHT, year="2029E"),
                        _snap(pe={"2026E": 34.9}), 20)

        assert r is not None and r["status"] == "missing"

    def test_skip_entries_are_ignored(self):
        assert td.evaluate("688498", {"name": "源杰科技", "skip": "口径不可比"}, _snap(), 20) is None


# ── 登记表与页面脱节 ─────────────────────────────────────────────────────────


class TestClaimStillOnPage:
    _HTML = ('<p>当前 <span id="s1-cur-pe26">~44x</span> 已高于乐观情景上沿（35x）</p>'
             '<script>var s="不该被搜到的当前 ~99x";</script>')

    def test_claim_split_by_tags_still_matches(self):
        """原句常被 <span> 水合锚点切断，去标签去空白后才比得上。"""
        assert td.claim_present(self._HTML, "当前 ~44x 已高于乐观情景上沿（35x）")

    def test_edited_page_no_longer_matches(self):
        assert not td.claim_present(self._HTML, "当前 ~44x 已低于保守情景下沿")

    def test_script_content_does_not_count(self):
        """页面 JS 里出现同样的字符串不算正文还在。"""
        assert not td.claim_present(self._HTML, "不该被搜到的当前 ~99x")

    def test_collect_reports_the_disconnect(self, tmp_path):
        pages, data = tmp_path / "pages", tmp_path / "data"
        pages.mkdir()
        data.mkdir()
        (pages / "p.html").write_text("<p>正文已经改写过了</p>", encoding="utf-8")
        (data / "300308-snapshot.json").write_text(json.dumps(_snap(pe={"2026E": 34.9})))
        (data / "t.json").write_text(json.dumps(
            {"stocks": {"300308": dict(INNOLIGHT, page="/p.html")}}), encoding="utf-8")

        got = td.collect(data_dir=data, thesis_file=data / "t.json", pages_dir=pages)

        assert len(got) == 1 and got[0]["status"] == "claim-gone"


# ── 只在状态变化时告警 ───────────────────────────────────────────────────────


class TestTransitions:
    _F = [{"code": "300308", "name": "中际旭创", "page": "/p.html", "status": "zone:2",
           "what": "正文的估值结论已被现价推翻", "detail": "d", "claim": "c"}]

    def test_new_problem_is_reported(self):
        changed, state = td.transitions(self._F, {})

        assert len(changed) == 1 and state == {"300308": "zone:2"}

    def test_unchanged_state_is_silent(self):
        """★核心：持续状态天天报，两周后就没人看了。"""
        changed, _ = td.transitions(self._F, {"300308": "zone:2"})

        assert changed == []

    def test_moving_to_another_band_is_reported(self):
        changed, _ = td.transitions(self._F, {"300308": "zone:3"})

        assert len(changed) == 1

    def test_recovery_is_reported(self):
        """问题消失同样值得知道——正文重新说得通了。"""
        changed, state = td.transitions([], {"300308": "zone:2"})

        assert len(changed) == 1 and changed[0]["status"] == "aligned"
        assert state == {}

    def test_recovered_then_quiet(self):
        changed, _ = td.transitions([], {})

        assert changed == []


# ── 告警正文 ─────────────────────────────────────────────────────────────────


class TestAlertHtml:
    def test_links_back_to_the_page(self):
        html = td.build_html(TestTransitions._F, date(2026, 8, 9))

        assert "/p.html" in html

    def test_quotes_the_original_sentence(self):
        html = td.build_html(TestTransitions._F, date(2026, 8, 9))

        assert "正文原句" in html and "c" in html

    def test_empty_yields_empty(self):
        assert td.build_html([], date(2026, 8, 9)) == ""


# ── 登记表本身的完整性 ───────────────────────────────────────────────────────


class TestRegistry:
    _REG = json.loads((Path.home() / "docs-site" / "data" / "report-thesis.json").read_text())
    _STOCKS = json.loads((_ROOT / "config" / "research_stocks.json").read_text())

    def test_covers_every_watchlist_stock(self):
        """★漏登记＝该页永远不被检查，且没有任何迹象。"""
        keys = {s.get("snapshot_key") or s.get("symbol") for s in self._STOCKS}

        assert keys == set(self._REG["stocks"])

    def test_no_silently_empty_entries(self):
        """无法登记要写明 skip 原因，否则看不出是漏了还是有意排除。"""
        for code, e in self._REG["stocks"].items():
            assert e.get("skip") or {"metric", "year", "asserted"} <= set(e), code

    def test_metrics_are_known(self):
        for code, e in self._REG["stocks"].items():
            if not e.get("skip"):
                assert e["metric"] in td.METRIC_FIELD, code

    def test_bands_are_ascending(self):
        """zone() 依赖升序；写反了会得出静默的错误结论。"""
        for code, e in self._REG["stocks"].items():
            b = e.get("bands")
            if b:
                assert b == sorted(b), code

    def test_every_claim_still_exists_in_its_page(self):
        """★登记表会自己烂掉：正文一被改，登记的句子就成了历史文物，
        此后每天拿一句页面上已不存在的话跟现价比——静默失效。"""
        pages = Path.home() / "docs-site" / "pages"
        for code, e in self._REG["stocks"].items():
            if e.get("skip"):
                continue
            html = (pages / e["page"].lstrip("/")).read_text(encoding="utf-8")

            assert td.claim_present(html, e["claim"]), f"{code} 的登记原句已不在页面里"

    def test_band_labels_match_band_count(self):
        for code, e in self._REG["stocks"].items():
            if e.get("bands") and e.get("band_labels"):
                assert len(e["band_labels"]) == len(e["bands"]), code
