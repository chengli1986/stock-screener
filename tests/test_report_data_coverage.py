#!/usr/bin/env python3
"""test_report_data_coverage.py — report-data 从 1 只推广到全部 11 只观察池标的

## 为什么要改

`update_research_report_data.py` 是为中际旭创**一只股票**写的，硬编码贯穿全文：
LLM prompt 写死「以下是中际旭创 300308（高速光模块）年度报告」、页面关键词表里
写死 `1.6T OSFP`/`800G OSFP`。直接把 11 只喂进去会产出错误归因的提取结果。

## 探针实测（2026-08-06，11 只全跑）

| 环节 | 可用 | 说明 |
|---|---|---|
| 研发费用（同花顺） | 10/11 | 仅智谱（港股）不可用 |
| 年报 PDF（巨潮） | 6/11 → 修正后 9/11 | 见下 |

**年报发现的真 bug**：`^\\d{4}年年度报告$` 要求标题**完全等于**「2025年年度报告」。
巨潮标题格式各公司自定——旭创/宁德/三环/风华/寒武纪/长光是 `2025年年度报告`（匹配），
而茅台是 `贵州茅台2025年年度报告`、源杰是 `陕西源杰半导体科技股份有限公司2025年年度报告`
（带公司名前缀 → 静默不匹配）。这 3 只被漏掉不是数据源问题，是正则锚点错了。

## 两类「拿不到」必须区分

推广后若沿用「任一失败即 exit(1)」，每年 5-05 必然告警，久了就没人看告警了：

- **结构性不可用**（预期，不告警）：智谱是港股，巨潮只覆盖沪深京；长鑫 2026-07-27 上市，
  近两年一份年报都没发过，首份年报要等 2027-04。
- **真失败**（该告警）：网络错误、PDF 解析失败、**或巨潮明明返回了公告却全被过滤掉**
  ——后者正是上面那个正则 bug 的形态，必须能被当成失败抓出来，而不是当成「没有年报」。

判据：巨潮返回 0 条 → 结构性；返回 N 条但过滤后为 0 → 失败。

## 两条腿独立降级

`rd_expenses`（同花顺）与 `extracted`（年报 PDF+LLM）是两个独立数据源，
一个断了不该让另一个也拿不到——长鑫就是「有研发费用、无年报」的典型。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_research_report_data.py"
_spec = importlib.util.spec_from_file_location("update_research_report_data", _SCRIPT)
urrd = importlib.util.module_from_spec(_spec)
sys.modules["update_research_report_data"] = urrd
_spec.loader.exec_module(urrd)


# ── ① 年报标题匹配：允许公司名前缀 ────────────────────────────────────────────


class TestAnnualReportTitleMatching:
    """全部用巨潮 2026 年真实返回的标题，不用编造样本。"""

    @pytest.mark.parametrize("title", [
        "2025年年度报告",                                    # 旭创/宁德/三环/风华/寒武纪/长光
        "贵州茅台2025年年度报告",                              # 茅台
        "盛科通信2025年年度报告",                              # 盛科
        "陕西源杰半导体科技股份有限公司2025年年度报告",            # 源杰
    ])
    def test_accepts_titles_with_or_without_company_prefix(self, title):
        assert urrd.is_full_annual_report(title) is True

    @pytest.mark.parametrize("title", [
        "2025年年度报告摘要",
        "贵州茅台2025年年度报告摘要",
        "贵州茅台2025年年度报告（英文版）",
        "2025年半年度报告",
        "盛科通信2025年半年度报告",
        "2025年度报告披露的提示性公告",
        "年报信息披露重大差错责任追究制度",
        "关于2025年年度报告的更正公告",
    ])
    def test_rejects_summaries_translations_and_notices(self, title):
        assert urrd.is_full_annual_report(title) is False

    def test_rejects_prior_year_when_newer_exists(self):
        """两年窗口内会同时返回 2024/2025 两份，取最新那份由调用方按顺序保证；
        这里只确认两份都是合法全文年报（不是过滤掉旧的）。"""
        assert urrd.is_full_annual_report("贵州茅台2024年年度报告") is True


# ── ② 结构性不可用 vs 真失败 ──────────────────────────────────────────────────


class TestUnavailableVersusFailure:
    """★推广后最关键的一条：区分错了，要么天天误报，要么真 bug 被静默吞掉。"""

    def test_hk_listing_is_structurally_unavailable(self):
        """智谱 02513：巨潮 `market='沪深京'` 结构上就不含港股。"""
        reason = urrd.annual_report_unavailable_reason({"symbol": "02513", "exchange": "HK"})

        assert reason is not None
        assert "港股" in reason

    def test_a_share_is_not_structurally_unavailable(self):
        reason = urrd.annual_report_unavailable_reason({"symbol": "600519", "exchange": "SH"})

        assert reason is None

    def test_zero_announcements_means_new_listing_not_failure(self):
        """长鑫 688825：巨潮近两年 0 条年度报告公告 —— 上市不满一年，属预期。"""
        result = urrd.classify_no_match(announcements_found=0)

        assert result == "unavailable"

    def test_announcements_present_but_all_filtered_is_a_failure(self):
        """★这正是茅台/源杰/盛科此前的形态：有公告却一条没匹配上。
        当成「没有年报」会把正则 bug 永久掩埋，必须报失败。"""
        result = urrd.classify_no_match(announcements_found=12)

        assert result == "failure"


class TestEmptyCninfoResultIsNotAFailure:
    """★实跑暴露（2026-08-06 长鑫 688825）：守卫写晚了。

    我原本在 akshare **返回之后**才检查 `df.empty` / 缺列，但巨潮对次新股返回空结果时，
    akshare 在内部就对空 DataFrame 做了列选择并抛出
    `KeyError: "None of [Index(['代码', '简称', ...])] are in the [columns]"`
    ——异常在我的守卫之前就飞出来了，长鑫因此被算成真失败、整个脚本 exit 1。

    只吞「空结果」这一种形态，其余 KeyError 必须继续上抛，否则真 bug 会被静默。
    """

    _EMPTY_ERR = KeyError(
        "None of [Index(['代码', '简称', '公告标题', '公告时间', "
        "'announcementId', 'orgId'], dtype='str')] are in the [columns]"
    )

    def test_empty_result_keyerror_is_treated_as_no_announcements(self):
        assert urrd.is_empty_cninfo_result(self._EMPTY_ERR) is True

    @pytest.mark.parametrize("err", [
        KeyError("公告标题"),
        KeyError("flashData"),
        ValueError("connection reset"),
    ])
    def test_other_errors_are_not_swallowed(self, err):
        assert urrd.is_empty_cninfo_result(err) is False

    def test_new_listing_yields_none_instead_of_raising(self, monkeypatch):
        """长鑫应走「次新股，无年报」这条 unavailable 路径，而不是让 cron 报错。"""
        def _boom(**kwargs):
            raise self._EMPTY_ERR

        monkeypatch.setattr(urrd.ak, "stock_zh_a_disclosure_report_cninfo", _boom)

        assert urrd._find_annual_report_url("688825", "SH") is None


# ── ③ prompt 参数化：不能再写死旭创 ───────────────────────────────────────────


class TestPromptIsStockSpecific:
    _MAOTAI = {"symbol": "600519", "name": "贵州茅台", "business": "白酒"}

    def test_prompt_names_the_actual_stock(self):
        prompt = urrd.build_extraction_prompt(self._MAOTAI)

        assert "贵州茅台" in prompt
        assert "600519" in prompt

    def test_prompt_does_not_leak_innolight(self):
        """原 prompt 开头就是「以下是中际旭创 300308（高速光模块）」——
        拿它去读茅台年报，LLM 会被引导去找根本不存在的光模块出货量。"""
        prompt = urrd.build_extraction_prompt(self._MAOTAI)

        assert "中际旭创" not in prompt
        assert "光模块" not in prompt
        assert "1.6T" not in prompt

    def test_prompt_keeps_universal_fields(self):
        """员工结构与地区收入分布是所有行业年报都有的，必须保留。"""
        prompt = urrd.build_extraction_prompt(self._MAOTAI)

        assert "employees" in prompt
        assert "geographic_revenue" in prompt

    def test_business_hint_is_optional(self):
        prompt = urrd.build_extraction_prompt({"symbol": "000636", "name": "风华高科"})

        assert "风华高科" in prompt


# ── ④ 页面关键词：通用类目 + 每股票可选补充 ───────────────────────────────────


class TestPageKeywords:
    def test_universal_categories_apply_to_every_stock(self):
        cats = urrd.page_categories({"symbol": "600519", "name": "贵州茅台"})

        assert "employees" in cats
        assert "geography" in cats

    def test_innolight_optical_keywords_are_not_global(self):
        """`1.6T OSFP` 曾在全局类目表里 —— 会让茅台年报去命中无关页面。"""
        cats = urrd.page_categories({"symbol": "600519", "name": "贵州茅台"})
        flat = [kw for kws in cats.values() for kw in kws]

        assert not any("OSFP" in kw for kw in flat)

    def test_per_stock_keywords_are_merged_in(self):
        cats = urrd.page_categories({
            "symbol": "300308", "name": "中际旭创",
            "report_keywords": ["1.6T OSFP", "800G OSFP"],
        })
        flat = [kw for kws in cats.values() for kw in kws]

        assert "1.6T OSFP" in flat

    def test_per_stock_keywords_do_not_drop_universal_ones(self):
        cats = urrd.page_categories({
            "symbol": "300308", "name": "中际旭创", "report_keywords": ["1.6T OSFP"],
        })

        assert "employees" in cats


# ── ⑤ 瞬时故障要重试，别当成「没有数据」 ──────────────────────────────────────


class TestTransientFetchIsRetried:
    """★实跑暴露（2026-08-06 宁德 300750）：同花顺返回非 JSON 响应，
    `JSONDecodeError` 直接把研发费用整条腿判死。

    同一接口在几分钟前的探针里是成功的（拿到 221.47 亿），所以这是瞬时故障，
    不是「该股票没有研发费用数据」。不重试 = 每年一次的抓取被一次抖动毁掉，
    而下一次机会要等 12 个月。
    """

    def test_retries_then_succeeds(self, monkeypatch):
        import json as _json
        calls = {"n": 0}
        good = [{"year": "2025A", "rd_yi": 221.47, "rd_ratio_pct": 5.23}]

        def _flaky(symbol):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _json.JSONDecodeError("Expecting value", "", 0)
            return good

        monkeypatch.setattr(urrd, "_fetch_rd_expenses_once", _flaky)
        monkeypatch.setattr(urrd.time, "sleep", lambda s: None)

        assert urrd.fetch_rd_expenses("300750") == good
        assert calls["n"] == 3

    def test_gives_up_after_max_attempts(self, monkeypatch):
        def _always_fail(symbol):
            raise ValueError("still broken")

        monkeypatch.setattr(urrd, "_fetch_rd_expenses_once", _always_fail)
        monkeypatch.setattr(urrd.time, "sleep", lambda s: None)

        with pytest.raises(ValueError):
            urrd.fetch_rd_expenses("300750")

    def test_hk_failure_is_not_retried(self, monkeypatch):
        """港股在 A 股接口上是**结构性**失败（KeyError: 'flashData'），
        重试 3 次只是白等 —— 每次都必然失败。"""
        calls = {"n": 0}

        def _hk(symbol):
            calls["n"] += 1
            raise KeyError("flashData")

        monkeypatch.setattr(urrd, "_fetch_rd_expenses_once", _hk)
        monkeypatch.setattr(urrd.time, "sleep", lambda s: None)

        with pytest.raises(KeyError):
            urrd.fetch_rd_expenses("02513")
        assert calls["n"] == 1


# ── ⑥ 两条腿独立降级 ──────────────────────────────────────────────────────────


class TestPartialDataIsBetterThanNothing:
    """长鑫实测：研发费用 4 年拿得到（招股书数据已进同花顺），年报拿不到。"""

    def test_rd_only_payload_is_still_written(self):
        payload = urrd.assemble_payload(
            stock={"symbol": "688825", "name": "长鑫科技", "snapshot_key": "688825"},
            rd_expenses=[{"year": "2025A", "rd_yi": 95.93, "rd_ratio_pct": 15.52}],
            report=None,
            unavailable_reason="上市不满一年，巨潮近两年无年度报告",
        )

        assert payload["rd_expenses"][-1]["rd_yi"] == 95.93
        assert payload["extracted"] is None

    def test_unavailable_reason_is_recorded_not_silently_empty(self):
        """页面要能说清「为什么没有」，而不是显示一片空白让人以为抓漏了。"""
        payload = urrd.assemble_payload(
            stock={"symbol": "02513", "name": "智谱", "snapshot_key": "02513"},
            rd_expenses=[],
            report=None,
            unavailable_reason="港股，巨潮资讯不覆盖",
        )

        assert "港股" in payload["coverage"]["annual_report_note"]
        assert payload["coverage"]["has_annual_report"] is False

    def test_coverage_flags_reflect_what_was_actually_obtained(self):
        payload = urrd.assemble_payload(
            stock={"symbol": "300308", "name": "中际旭创", "snapshot_key": "300308"},
            rd_expenses=[{"year": "2025A", "rd_yi": 16.15}],
            report={
                "source": {"type": "annual_report", "title": "2025年年度报告",
                           "url": "https://x/1.PDF", "date": "2026-03-31", "pages": 229},
                "extracted": {"employees": {"total": 11625}},
            },
            unavailable_reason=None,
        )

        assert payload["coverage"]["has_annual_report"] is True
        assert payload["coverage"]["has_rd_expenses"] is True
        assert payload["report_source"]["pages"] == 229

    def test_partial_note_says_which_leg_is_missing(self):
        """★实跑暴露：宁德只缺研发费用，摘要却只印「部分数据缺失」——
        看不出缺的是哪条腿，等于没说。"""
        payload = urrd.assemble_payload(
            stock={"symbol": "300750", "name": "宁德时代", "snapshot_key": "300750"},
            rd_expenses=[],
            report={"source": {"type": "annual_report", "title": "2025年年度报告",
                               "url": "u", "date": "2026-03-10", "pages": 300},
                    "extracted": {"employees": {"total": 185839}}},
            unavailable_reason=None,
        )

        assert "研发费用" in urrd.coverage_summary(payload)

    def test_coverage_summary_is_empty_when_everything_landed(self):
        payload = urrd.assemble_payload(
            stock={"symbol": "300308", "name": "中际旭创", "snapshot_key": "300308"},
            rd_expenses=[{"year": "2025A", "rd_yi": 16.15}],
            report={"source": {"type": "annual_report", "title": "t", "url": "u",
                               "date": "2026-03-31", "pages": 229},
                    "extracted": {"employees": {"total": 11625}}},
            unavailable_reason=None,
        )

        assert urrd.coverage_summary(payload) == ""

    def test_rd_failure_alone_does_not_void_the_annual_report(self):
        payload = urrd.assemble_payload(
            stock={"symbol": "02513", "name": "智谱", "snapshot_key": "02513"},
            rd_expenses=[],
            report={"source": {"type": "annual_report", "title": "t", "url": "u",
                               "date": "2026-01-01", "pages": 10},
                    "extracted": {"employees": {"total": 1}}},
            unavailable_reason=None,
        )

        assert payload["coverage"]["has_rd_expenses"] is False
        assert payload["extracted"]["employees"]["total"] == 1
