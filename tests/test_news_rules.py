#!/usr/bin/env python3
"""test_news_rules.py — 新闻/公告的分层判定

标题全部取自 2026-08-11 的真实抓取结果（见 spec 第 7 节）。
层序：major > substantive > news > procedural > technical > sector。
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


class TestTechnicalCapitalFlowGaps:
    """★2026-08-11 二次审查：肉眼查真实页面发现漏配——三环集团「相关新闻」层
    7 条里 5 条是资金流榜单，长光华芯页 4 条里 3 条。标题全部取自当天真实
    抓取结果。原正则只认「资金净流入/净流出」这种固定搭配，命不中下面这些。"""

    def test_capital_outflow_ranking_no_jing(self):
        """「资金流出榜」没有「净」字，也没有「资金净流出」那种固定搭配。"""
        assert nr.classify(
            news("电子行业资金流出榜：寒武纪、中微公司等净流出资金居前")) == "technical"

    def test_net_inflow_after_the_word_capital(self):
        """「净流入」在「资金」之后而非之前——「特大单净流入资金超」。"""
        assert nr.classify(news("38股特大单净流入资金超2亿元")) == "technical"

    def test_margin_trading_investor(self):
        assert nr.classify(news("56股获融资客大手笔买入")) == "technical"

    def test_leveraged_capital(self):
        assert nr.classify(news("杠杆资金连续五日加仓创业板股")) == "technical"

    def test_capital_outflow_with_words_in_between(self):
        """「资金」和「流出」之间隔着「今日」，不是紧邻搭配。"""
        assert nr.classify(news("192.08亿元资金今日流出电子股")) == "technical"

    def test_announcement_guard_still_holds_after_regex_expansion(self):
        """★补词不能误伤『公告不进技术面层』这条既有守卫。"""
        assert nr.classify(
            ann("关于公司股票主力资金净流入情况的说明公告")) == "substantive"

    def test_buyback_and_share_increase_not_swept_into_technical(self):
        """★不要把「回购」「增持」这类大事层关键词误纳入技术面。"""
        assert nr.classify(ann("关于收到董事长提议回购公司股份的公告")) == "major"
        assert nr.classify(ann("董事會關於董事增持股份的公告")) == "major"


class TestNewsLayer:
    def test_plain_news_is_news_layer(self):
        assert nr.classify(news("Bernstein starts coverage of China AI labs, favors Z.ai")) == "news"


class TestSectorLayerOrderAndLabel:
    def test_sector_is_last_in_layer_order(self):
        assert nr.LAYER_ORDER[-1] == "sector"

    def test_sector_label(self):
        assert nr.LAYER_LABEL["sector"] == "板块与市场"


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


# ── sector 层：新闻相关性降权 ─────────────────────────────────────────────────


# ★2026-08-11 二审 Finding 1：这些别名与 config/research_stocks.json 的当前
# 真实值保持一致——中文短别名（长光/三环/旭创/盛科/源杰/风华/宁德/长鑫）已
# 删除（对抗测试证明会误命中同名实体，实测量化贡献又是 0），只留零误命中
# 风险的拉丁专名 / 已知有真实贡献的「茅台」。ZHIPU 补了 Finding 2 的
# "2513"/"Knowledge Atlas"。
ZHIPU = {"name": "智谱", "symbol": "02513",
         "aliases": ["Zhipu", "Z.ai", "ZhipuAI", "智谱AI", "2513", "Knowledge Atlas"]}
CHANGGUANG = {"name": "长光华芯", "symbol": "688048", "aliases": []}
SANHUAN = {"name": "三环集团", "symbol": "300408", "aliases": []}


class TestSectorLayer:
    """★用户 2026-08-11 看真实页面拍板：长光华芯「相关新闻」4 条没有一条提到
    长光华芯（全是「107只科创板股票跻身百元股阵营」这类板块级噪音）；三环 7
    条里只有 2 条真相关；智谱 7 条里 3 条相关（2 条是 BNB 和比特币）。裁定：
    降权到末尾的 sector 层，不删。标题全部取自 brief 里列出的当天真实抓取。

    ★别名是必需的，不能只用公司名——智谱的相关新闻全是英文标题，不会出现
    「智谱」二字。这组用例专门用真实智谱标题验证：只按「智谱」两字匹配会把
    3 条真相关的也降权掉，这正是本组测试要守住的。
    """

    # 7 条智谱真实标题，brief 已标注哪些相关
    def test_zhipu_bernstein_coverage_stays_in_news(self):
        assert nr.classify(
            news("Bernstein starts coverage of China's AI labs, favors Z.ai over Minimax"),
            ZHIPU) == "news"

    def test_zhipu_moonshot_deepseek_stays_in_news(self):
        assert nr.classify(
            news("China's Moonshot, Z.AI, and DeepSeek are challenging U.S. AI labs"),
            ZHIPU) == "news"

    def test_zhipu_share_sale_stays_in_news(self):
        assert nr.classify(
            news("Zhipu Unveils Open-Source AI as $4 Billion Share Sale Lifts Stock"),
            ZHIPU) == "news"

    def test_zhipu_tencent_workbuddy_demoted_to_sector(self):
        assert nr.classify(
            news("Tencent's WorkBuddy Sparks AI Turnaround Hopes After Stock Rout"),
            ZHIPU) == "sector"

    def test_zhipu_performance_gap_demoted_to_sector(self):
        assert nr.classify(
            news("Chinese AI Cuts U.S. Performance Gap to Record 6% in June"),
            ZHIPU) == "sector"

    def test_zhipu_bnb_demoted_to_sector(self):
        assert nr.classify(
            news("Binance's BNB Coin May Drop Toward $500 Next: Here's Why"),
            ZHIPU) == "sector"

    def test_zhipu_bitcoin_demoted_to_sector(self):
        assert nr.classify(
            news("Bitcoin Price Warning: Kimi K3-Led AI Selloff Could Push BTC Below $60K"),
            ZHIPU) == "sector"

    def test_zhipu_count_three_relevant_four_sector(self):
        """汇总断言：7 条里恰好 3 条留 news、4 条进 sector，对应 brief 的裁定。"""
        titles = [
            "Bernstein starts coverage of China's AI labs, favors Z.ai over Minimax",
            "China's Moonshot, Z.AI, and DeepSeek are challenging U.S. AI labs",
            "Zhipu Unveils Open-Source AI as $4 Billion Share Sale Lifts Stock",
            "Tencent's WorkBuddy Sparks AI Turnaround Hopes After Stock Rout",
            "Chinese AI Cuts U.S. Performance Gap to Record 6% in June",
            "Binance's BNB Coin May Drop Toward $500 Next: Here's Why",
            "Bitcoin Price Warning: Kimi K3-Led AI Selloff Could Push BTC Below $60K",
        ]
        got = [nr.classify(news(t), ZHIPU) for t in titles]

        assert got.count("news") == 3
        assert got.count("sector") == 4

    # 长光华芯真实标题：3 条科创板榜单新闻，一条都不提「长光」
    def test_changguang_stock_price_ranking_demoted(self):
        assert nr.classify(news("23只科创板股融资余额增加超5000万元"), CHANGGUANG) == "sector"

    def test_changguang_hundred_yuan_club_demoted(self):
        assert nr.classify(news("107只科创板股票跻身百元股阵营"), CHANGGUANG) == "sector"

    def test_changguang_average_price_demoted(self):
        assert nr.classify(news("科创板平均股价52.63元，107股股价超百元"), CHANGGUANG) == "sector"

    # 三环真实标题：2 条含「三环集团」，真相关，留在原规则判定的层
    def test_sanhuan_placing_stays_relevant(self):
        got = nr.classify(
            news("三环集团部分行使超额配股权，预计募资8.79亿港元"), SANHUAN)

        assert got != "sector"

    def test_sanhuan_board_reshuffle_stays_relevant(self):
        got = nr.classify(news("三环集团完成董事会换届：马艳红为总经理"), SANHUAN)

        assert got != "sector"

    # 技术面 vs sector 的优先级：既不提公司名、又命中技术面正则的新闻 → sector
    def test_technical_looking_headline_without_company_name_goes_to_sector_not_technical(self):
        """★brief 给的例子：「电子行业资金流出榜」既不含长光华芯的别名，又命中
        技术面正则（'资金\\S{0,6}流出'）。归 sector 而不是 technical——判定顺序
        是先查相关性，不相关直接进 sector，不再往下走技术面判断。"""
        got = nr.classify(news("电子行业资金流出榜：寒武纪、中微公司等净流出资金居前"),
                          CHANGGUANG)

        assert got == "sector"

    # 公告一律不参与 sector 判定，即便不提公司名
    def test_announcement_never_demoted_to_sector_even_without_company_name(self):
        """★『只对新闻生效，公告一律不参与』——公告是本公司自己发的，天然相关，
        哪怕标题本身没写出公司名（如巨潮标准格式的董事会公告）也不该被降权。"""
        got = nr.classify(ann("关于召开2026年第三次临时股东会的通知"), CHANGGUANG)

        assert got != "sector"

    # 向后兼容：company=None 时行为与新增 sector 层之前完全一致
    def test_company_none_never_produces_sector(self):
        """★接口契约：`company` 默认 None，行为与现在完全一致——不会产出
        sector，Task 1/2/4 的既有测试因此一条都不用改。"""
        got = nr.classify(news("Tencent's WorkBuddy Sparks AI Turnaround Hopes"))

        assert got != "sector"


class TestSectorLayerShortAliasFalsePositives:
    """★2026-08-11 二审 Finding 1：审查者用真实存在的同名实体做对抗测试，
    证明中文短别名（长光/三环/旭创/盛科/源杰/风华/宁德/长鑫）会把无关公司的
    新闻误判为「相关」（不降权到 sector）。量化过这些别名在 119 条真实标题里
    的实际贡献——除了「茅台」（3 条）和拉丁专名，全部是 0，删除后不损失任何
    真实召回，只消除误命中风险。这组用例用 config 里当前真实的（已删除短别名的）
    ZHIPU/CHANGGUANG/SANHUAN 验证这些同名实体标题现在会被正确降权到 sector。
    """

    def test_changguang_satellite_company_is_sector_not_relevant(self):
        """「长光卫星」是另一家公司，不是长光华芯。"""
        got = nr.classify(news("长光卫星完成新一轮融资"), CHANGGUANG)

        assert got == "sector"

    def test_changchen_ipo_is_sector_not_relevant(self):
        """「长光辰芯」也是另一家公司。"""
        got = nr.classify(news("长光辰芯科创板IPO获受理"), CHANGGUANG)

        assert got == "sector"

    def test_sanhuan_road_traffic_is_sector_not_relevant(self):
        """「三环路」是北京的道路，不是三环集团。"""
        got = nr.classify(news("北京三环路早高峰拥堵指数创新高"), SANHUAN)

        assert got == "sector"

    def test_sanhuan_shares_another_company_is_sector_not_relevant(self):
        """「三环股份」是另一家上市公司（同名不同司），不是三环集团。"""
        got = nr.classify(news("三环股份拟发行可转债"), SANHUAN)

        assert got == "sector"

    def test_ningde_city_government_is_sector_not_relevant(self):
        """「宁德市政府」是地级市政府，不是宁德时代。用当前真实（不含中文
        「宁德」别名，只保留 CATL）的公司信息验证。"""
        company = {"name": "宁德时代", "symbol": "300750", "aliases": ["CATL"]}
        got = nr.classify(news("宁德市政府与多家企业签署战略合作协议"), company)

        assert got == "sector"

    def test_fenghua_fund_manager_column_is_sector_not_relevant(self):
        """「风华正茂」是常见成语，用在基金经理专栏标题里，不是风华高科。"""
        company = {"name": "风华高科", "symbol": "000636", "aliases": []}
        got = nr.classify(news("风华正茂：A股新生代基金经理观察"), company)

        assert got == "sector"

    def test_shengke_new_materials_is_sector_not_relevant(self):
        """「盛科新材料」是另一家公司，不是盛科通信。"""
        company = {"name": "盛科通信", "symbol": "688702", "aliases": []}
        got = nr.classify(news("盛科新材料完成B轮融资"), company)

        assert got == "sector"

    def test_moutai_town_tradeoff_is_accepted_and_documented(self):
        """★「茅台」保留是刻意取舍（真实贡献 3 条，量化见 config 里的
        news_aliases_note），代价是「茅台镇」这类同地名新闻会被误判相关——
        这条不是 bug，是记录在案的已知行为，测试只是把它钉住，不是在追求
        消除它。"""
        got = nr.classify(news("茅台镇多家酒企集体提价"), {
            "name": "贵州茅台", "symbol": "600519", "aliases": ["茅台"]})

        assert got != "sector"  # 已知代价：这是刻意接受的假阳性，不是缺陷


class TestSectorLayerHkexLeadingZero:
    """★2026-08-11 二审 Finding 2：港交所代码带前导零（02513），但外媒引用
    港股代码几乎不带前导零（SEHK:2513），字符串子串匹配天然对不上——这是
    真实假阴性，`02513-news-raw.jsonl` 里智谱自己的英文通稿因此被误判
    sector。"""

    def test_real_hkex_leading_zero_headline_stays_relevant(self):
        """真实标题：智谱自己的英文名 + 自己的港股代码（无前导零引用）。"""
        got = nr.classify(news(
            "Knowledge Atlas Technology (SEHK:2513) Launches Share Sale "
            "And Opens GLM 5.2 To Developers"), ZHIPU)

        assert got != "sector"

    def test_leading_zero_stripped_variant_matches_even_without_alias(self):
        """不靠「2513」这个别名兜底，单纯代码去前导零也要能匹配——
        验证的是 `_mentions_company` 的代码逻辑，不是配置表补的别名。"""
        company = {"name": "智谱", "symbol": "02513", "aliases": []}
        got = nr.classify(news("SEHK:2513 completes GLM share placement"), company)

        assert got != "sector"

    def test_a_share_leading_zeros_not_over_stripped(self):
        """★不能把这条处理用到 A 股代码上产生新问题：`000636` 去零后是
        `636`，只有 3 位，低于 4 位阈值，不启用，不会把含「636」的无关文本
        误判成风华高科相关。"""
        company = {"name": "风华高科", "symbol": "000636", "aliases": []}
        got = nr.classify(news("今日沪指跌0.636%，两市成交额6360亿元"), company)

        assert got == "sector"


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
