#!/usr/bin/env python3
"""news_rules.py — 研报页「相关新闻」的分层与聚合规则（纯函数，无 IO）

## 为什么是排序而不是过滤（用户 2026-08-11）

> 「关于股价，资金流动，龙虎榜这些，明显属于技术分析的，我肯定是放到末尾的，
>   我这个页面主要还是基本面分析为主的」

所以六层**只排序不删除**：major > substantive > news > procedural > technical > sector。
sector（板块与市场，2026-08-11 新增）放最末尾：一条新闻若标题不含公司名/代码/
别名任何一个，判为与本公司不直接相关，降权到这层，不删除。

## 为什么公告不进技术面层

公告标题若含「主力资金净流入」等技术面关键词，会被 `_TECHNICAL` 规则命中。但公告
是公司一手披露（如「关于股票主力资金净流入情况的说明公告」），内容本就不涉及交易面
数据，故应返回 substantive 而非 technical。因此技术面判定**只对新闻生效**。
"""

import re

# 大事：用户点进页面最想第一眼看到的
#
# ★港股披露易公告是繁体。规则原为纯简体，实测「股份購回」「盈利警告」
# 「業績預告」「須予披露的交易」等 HK 常用语全部漏判，落到 substantive——
# 唯一的港股智谱「重要事项」层因此实质失效。以下补的繁体变体只新增命中，
# 不动任何既有（简体/A 股）分支，A 股判定行为不变。
_MAJOR = re.compile(
    r"(业绩预告|业绩快报|业绩预亏|业绩预增|業績預告|業績快報|盈利警告"
    r"|减持|增持|回购|購回|減持"
    r"|权益变动|要约收购|收购|收購|重组|合并|合併"
    r"|須予披露的交易|關連交易"
    r"|重大合同|中标|重大资产"
    r"|立案|处罚|诉讼|訴訟|仲裁"
    r"|解禁|限售股上市流通"
    r"|停牌|复牌|復牌|退市风险|清盤)"
)

# 程序性：走流程必须发，信息量低
_PROCEDURAL = re.compile(
    r"(法律意见书|核查意见|保荐|督导"
    r"|月报表|月報表|翌日披露"
    r"|会议资料|会议通知|会议材料"
    r"|独立董事|管理办法|管理制度|工作制度|章程"
    r"|募集资金存放|内部控制|会计师事务所|信用评级|受托管理事务)"
)

# 技术面：**只对新闻生效**
# 港股新闻多为英文，繁体命中优先级低，但补几个明显的以免漏判。
#
# ★2026-08-11 二次审查：肉眼查真实页面发现严重漏配——三环集团「相关新闻」层
# 7 条里 5 条是资金流榜单（如「电子行业资金流出榜」「38股特大单净流入资金超
# 2亿元」），长光华芯页更明显（4 条里 3 条）。原正则只认「资金净流入/净流出」
# 这种固定搭配，命不中「资金流出榜」（无「净」字）、「特大单净流入资金超」
# （「资金」在「净流入」之后而非之前）、「融资客」「杠杆资金」这类同属技术面
# 但措辞更松散的说法。下面按真实标题补齐，同时保留原有具体搭配不删——
# 新增只扩大命中面，不影响已有测试。
_TECHNICAL = re.compile(
    r"(龙虎榜|主力资金|资金净流入|资金净流出|资金流向|北向资金"
    r"|龍虎榜|資金淨流入|資金淨流出|北向資金"
    r"|涨停|跌停|封板|异动|盘中|强势股|大宗交易|融资融券|融资客|杠杆资金"
    r"|漲停|異動|盤中|強勢股"
    r"|排行|榜单|净流入超|净流出超"
    r"|净流入|净流出|资金\S{0,6}流出|资金\S{0,6}流入"
    r"|\d+\s*只股|多股|个股.{0,6}净流)"
)


def _mentions_company(title: str, company: dict) -> bool:
    """标题是否含公司名 / 股票代码 / 别名之一，latin 字符不区分大小写。

    ★用 `in` 判断而非正则——别名里的 `Z.ai` 这类字符串若走正则，`.` 是通配符，
    会把 `Zxai`、`Z1ai` 也算命中。全部走字符串子串匹配，`.lower()` 对中文字符
    是恒等操作，同一次 `.lower()` 对 latin/CJK 混合标题都安全。

    ★2026-08-11 二审 Finding 2：港股代码带前导零，`"02513" in "...SEHK:2513..."`
    是 False——外媒引用港股代码几乎不带前导零，这是真实假阴性（智谱自己的英文
    通稿因此被误判 sector）。补一个去前导零的变体，但**只在去零后仍 ≥4 位时
    才启用**，不看 `exchange` 字段（company 目前不带这个字段，两种做法二选一，
    选这个是因为不用多传参数）：A 股代码本身没有前导零（`300408` 类）或去零后
    很短（`000636` → `636` 只有 3 位），天然被这条阈值挡住，不会把 `636` 这种
    短数字串也当成可信代码去匹配，误伤一堆含「636」的无关文本。
    """
    hay = title.lower()
    symbol = str(company.get("symbol") or "")
    stripped = symbol.lstrip("0")
    needles = [company.get("name"), symbol, *(company.get("aliases") or [])]
    if len(stripped) >= 4:
        needles.append(stripped)
    return any(n and str(n).lower() in hay for n in needles)


def classify(item: dict, company: dict | None = None) -> str:
    """返回 major / substantive / news / procedural / technical / sector 之一。

    判定顺序即优先级：**相关性先判**（仅新闻、仅当传入 company），不相关直接
    降权到 sector，不再走大事/程序性/技术面这些规则——一条既不提这家公司、
    又命中技术面关键词的新闻（如「电子行业资金流出榜」），归 sector 而不是
    technical：它连这只股票都不是在讲，比「在讲这只股票的资金流」更远。
    这是用户 2026-08-11 拍板的判定顺序（见 news_rules 设计笔记）。

    大事其次判：避免「XX 证券关于回购的核查意见」被程序性规则先截走——
    那会让一条重磅掉到第 4 层。

    ``company`` 默认 None，此时行为与新增 sector 层之前完全一致（不会产出
    sector），这样调用方不传 company 时既有测试一条不用改。
    """
    title = str(item.get("title") or "")
    category = str(item.get("category") or "")
    is_announcement = item.get("kind") == "announcement"
    haystack = title + " " + category

    if company and not is_announcement and not _mentions_company(title, company):
        return "sector"

    if _MAJOR.search(haystack):
        return "major"
    if _PROCEDURAL.search(haystack):
        return "procedural"
    if is_announcement:
        return "substantive"
    if _TECHNICAL.search(title):
        return "technical"
    return "news"


LAYER_ORDER = ("major", "substantive", "news", "procedural", "technical", "sector")

LAYER_LABEL = {
    "major": "重要事项",
    "substantive": "公司公告",
    "news": "相关新闻",
    "procedural": "程序性文件",
    "technical": "交易与资金面",
    "sector": "板块与市场",
}


# 可聚合的事件关键词。只列**会被拆成多条连续公告**的那些——
# 实测三环集团一次回购发了 6 条（方案/首次/进展/结果/贷款承诺函/前十名股东持股）。
_EVENT_KEYS = ("回购", "减持", "增持", "权益变动", "业绩预告",
               "股东会", "董事会", "解禁", "限售股上市流通", "配售")


def _event_key(item: dict):
    """同事件的分组键：(来源类型, 事件关键词)。取不到关键词返回 None＝不参与聚合。

    带上来源类型，是因为公告与新闻不该合并——一个是一手披露、一个是二手报道，
    混在一起会让「共 N 条」这个数字失去意义。
    """
    title = str(item.get("title") or "")
    for k in _EVENT_KEYS:
        if k in title:
            return (item.get("kind"), k)
    return None


def group_events(items: list[dict]) -> list[dict]:
    """同事件聚成一条，保留最新那条为可见项，其余进 `group_members`。

    返回顺序：与传入顺序一致（按各组最新那条的原始位置）。排序由调用方负责。
    """
    groups: dict = {}
    order: list = []
    for it in items:
        key = _event_key(it)
        bucket = key if key else ("__single__", id(it))
        if bucket not in groups:
            groups[bucket] = []
            order.append(bucket)
        groups[bucket].append(it)

    out = []
    for bucket in order:
        members = sorted(groups[bucket], key=lambda x: str(x.get("date") or ""), reverse=True)
        head = dict(members[0])
        head["group_count"] = len(members)
        head["group_members"] = members[1:]
        out.append(head)
    return out
