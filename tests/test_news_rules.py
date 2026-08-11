#!/usr/bin/env python3
"""test_news_rules.py — 新闻/公告的分层判定

标题全部取自 2026-08-11 的真实抓取结果（见 spec 第 7 节）。
层序：major > substantive > news > procedural > technical。
"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("news_rules", _ROOT / "scripts" / "news_rules.py")
nr = importlib.util.module_from_spec(_spec)
sys.modules["news_rules"] = nr
_spec.loader.exec_module(nr)


def ann(title, category=None):
    return {"kind": "announcement", "title": title, "category": category}


def news(title):
    return {"kind": "news", "title": title, "category": None}


class TestMajor:
    """大事置顶。这些是用户点进页面最想第一眼看到的。"""

    def test_earnings_forecast(self):
        assert nr.classify(ann("2026年半年度业绩预告")) == "major"

    def test_share_reduction(self):
        assert nr.classify(ann("盛科通信关于持股5%以上股东权益变动触及5%刻度的提示性公告")) == "major"

    def test_buyback(self):
        assert nr.classify(ann("关于收到董事长提议回购公司股份的公告")) == "major"


class TestSubstantive:
    """实质但非大事的公告。"""

    def test_board_election(self):
        assert nr.classify(ann("关于完成董事会换届选举及聘任高级管理人员的公告")) == "substantive"

    def test_shareholder_meeting_resolution(self):
        assert nr.classify(ann("盛科通信2026年第一次临时股东会决议公告")) == "substantive"


class TestProcedural:
    """★程序性文件走流程必须发，信息量低，用户明确要求排到后面。"""

    def test_legal_opinion(self):
        assert nr.classify(ann("北京市金杜律师事务所上海分所关于苏州盛科通信股份有限公司2026年第一次临时股东会之法律意见书")) == "procedural"

    def test_sponsor_check(self):
        assert nr.classify(ann("中国国际金融股份有限公司关于苏州盛科通信股份有限公司增加2026年度日常关联交易预计额度的核查意见")) == "procedural"

    def test_meeting_material(self):
        assert nr.classify(ann("盛科通信2026年第一次临时股东会会议资料")) == "procedural"

    def test_hk_monthly_return_via_category(self):
        """★港股披露易自带官方分类，比猜标题可靠——优先用 category。"""
        assert nr.classify(ann("截至二零二六年七月三十一日止月份之股份發行人的證券變動月報表",
                               category="月報表")) == "procedural"

    def test_hk_next_day_disclosure_via_category(self):
        assert nr.classify(ann("(經修訂) 翌日披露報表", category="翌日披露報表 - [其他]")) == "procedural"


class TestHkSubstantive:
    def test_hk_placing_is_substantive(self):
        assert nr.classify(ann("完成根據一般授權配售新H股",
                               category="公告及通告 - [配售]")) == "substantive"


class TestTechnical:
    """★用户原话：股价/资金流/龙虎榜明显属于技术分析，肯定放到末尾。"""

    def test_dragon_tiger_list(self):
        assert nr.classify(news("中际旭创300308龙虎榜数据07-28")) == "technical"

    def test_capital_flow_ranking(self):
        assert nr.classify(news("28只科创板个股主力资金净流入超亿元")) == "technical"

    def test_strong_stock_tracking(self):
        assert nr.classify(news("强势股追踪 主力资金连续5日净流入162股")) == "technical"

    def test_daily_capital_flow(self):
        assert nr.classify(news("8月7日科创板主力资金净流入104.35亿元")) == "technical"


class TestNewsLayer:
    def test_plain_news_is_news_layer(self):
        assert nr.classify(news("Bernstein starts coverage of China AI labs, favors Z.ai")) == "news"


class TestPriority:
    def test_major_beats_procedural(self):
        """★同时命中「回购」与「核查意见」时，大事优先——否则重磅会掉到第 4 层。"""
        got = nr.classify(ann("中信证券关于中际旭创回购公司股份的核查意见"))

        assert got == "major"

    def test_major_beats_all_other_rules(self):
        """★大事规则最先判，避免重磅被其它规则先截走。

        这条用例验证的是『大事优先』这个优先级，不验证『公告不进技术面层』。
        那个标题含『回购』会被 _MAJOR 先命中，走不到 is_announcement 的守卫。
        """
        got = nr.classify(ann("关于取得金融机构股票回购贷款承诺函的公告"))

        assert got in ("major", "substantive")

    def test_announcement_guard_blocks_technical_layer(self):
        """★is_announcement 守卫确保公告不进技术面层。

        即便标题含『主力资金』这类技术面关键词，如果是公告，
        也应该返回 substantive 而非 technical。这条守卫被删掉时本测试会红。
        """
        got = nr.classify(ann("关于公司股票主力资金净流入情况的说明公告"))

        assert got == "substantive"


# ── 同事件聚合 ───────────────────────────────────────────────────────────────


def _a(title, date):
    return {"kind": "announcement", "title": title, "date": date,
            "url": "https://x/" + date + title[:4], "category": None}


class TestHkMajorTraditional:
    """★港股披露易公告是繁体，_MAJOR 原为纯简体正则，实测「股份購回」「盈利警告」
    「業績預告」「須予披露的交易」等 HK 常用大事标题全部漏判到 substantive——
    池子里唯一的港股智谱「重要事项」层因此实质失效。02513 抓到的公告目前都不是
    大事，标题取自 HKEX 标准公告用语（brief 列出的清单）。"""

    def test_share_repurchase_traditional(self):
        assert nr.classify(ann("建議股份購回一般性授權")) == "major"

    def test_profit_warning(self):
        assert nr.classify(ann("盈利警告")) == "major"

    def test_earnings_forecast_traditional(self):
        assert nr.classify(ann("截至二零二六年六月三十日止年度之業績預告")) == "major"

    def test_earnings_flash_traditional(self):
        assert nr.classify(ann("二零二六年第二季度業績快報")) == "major"

    def test_share_reduction_traditional(self):
        assert nr.classify(ann("關於主要股東減持股份的公告")) == "major"

    def test_share_accumulation_already_matched(self):
        """增持 简繁同形，正则未改动本已可命中——补测试锁定行为，不算本轮新增。"""
        assert nr.classify(ann("董事會關於董事增持股份的公告")) == "major"

    def test_discloseable_transaction(self):
        assert nr.classify(ann("須予披露的交易")) == "major"

    def test_connected_transaction(self):
        assert nr.classify(ann("持續關連交易")) == "major"

    def test_acquisition_traditional(self):
        assert nr.classify(ann("有關收購目標公司之非常重大收購事項")) == "major"

    def test_merger_traditional(self):
        assert nr.classify(ann("有關建議合併之公告")) == "major"

    def test_suspension_already_matched(self):
        """停牌 简繁同形——补测试锁定行为，不算本轮新增。"""
        assert nr.classify(ann("停牌")) == "major"

    def test_resumption_traditional(self):
        assert nr.classify(ann("復牌")) == "major"

    def test_litigation_traditional(self):
        assert nr.classify(ann("有關訴訟之最新進展")) == "major"

    def test_arbitration_already_matched(self):
        """仲裁 简繁同形——补测试锁定行为，不算本轮新增。"""
        assert nr.classify(ann("有關仲裁程序之公告")) == "major"

    def test_winding_up_traditional(self):
        assert nr.classify(ann("清盤呈請")) == "major"

    def test_a_share_behavior_unchanged(self):
        """★不该破坏 A 股既有判定：这条含「关联交易」（简体，未被本轮加入
        _MAJOR）且含「核查意见」，本应走 _PROCEDURAL，不能因繁体新增被带偏。"""
        got = nr.classify(ann(
            "中国国际金融股份有限公司关于苏州盛科通信股份有限公司"
            "增加2026年度日常关联交易预计额度的核查意见"))

        assert got == "procedural"


class TestHkTechnicalTraditional:
    """低优先级（港股新闻多为英文），但补几个明显的繁体技术面词，避免漏判。"""

    def test_dragon_tiger_list_traditional(self):
        assert nr.classify(news("騰訊控股龍虎榜資金流向")) == "technical"

    def test_northbound_capital_traditional(self):
        assert nr.classify(news("北向資金連續三日淨流入")) == "technical"

    def test_limit_up_traditional(self):
        assert nr.classify(news("多隻港股今日漲停")) == "technical"


class TestGroupEvents:
    """★实测三环集团一次回购产生 6 条公告，不聚合会在列表里连续刷屏。"""

    _BUYBACK = [
        _a("关于2026年第二期回购公司股份方案的公告暨回购股份报告书", "2026-07-21"),
        _a("关于首次回购公司股份的公告", "2026-07-22"),
        _a("关于首次回购公司股份暨回购股份进展的公告", "2026-08-03"),
        _a("关于股份回购结果暨股份变动的公告", "2026-08-05"),
        _a("关于取得金融机构股票回购贷款承诺函的公告", "2026-07-25"),
    ]

    def test_collapses_to_one_row(self):
        got = nr.group_events(self._BUYBACK)

        assert len(got) == 1

    def test_keeps_the_latest_as_the_visible_one(self):
        got = nr.group_events(self._BUYBACK)

        assert got[0]["date"] == "2026-08-05"

    def test_reports_how_many_were_collapsed(self):
        got = nr.group_events(self._BUYBACK)

        assert got[0]["group_count"] == 5

    def test_members_are_retained_for_expansion(self):
        """折叠的不能丢——用户要能展开看全部。"""
        got = nr.group_events(self._BUYBACK)

        assert len(got[0]["group_members"]) == 4

    def test_different_events_stay_separate(self):
        items = [_a("关于首次回购公司股份的公告", "2026-07-22"),
                 _a("2026年半年度业绩预告", "2026-07-14")]

        assert len(nr.group_events(items)) == 2

    def test_ungrouped_item_has_count_one(self):
        got = nr.group_events([_a("2026年半年度业绩预告", "2026-07-14")])

        assert got[0]["group_count"] == 1 and got[0]["group_members"] == []

    def test_news_and_announcement_never_group_together(self):
        """★不同来源的同关键词不该合并：公告是一手披露，新闻是二手报道，
        合并会让「共 N 条」这个数字失去意义。"""
        items = [_a("关于首次回购公司股份的公告", "2026-07-22"),
                 {"kind": "news", "title": "中际旭创拟回购80亿元", "date": "2026-07-29",
                  "url": "https://y/1", "category": None}]

        assert len(nr.group_events(items)) == 2
