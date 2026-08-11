#!/usr/bin/env python3
"""news_rules.py — 研报页「相关新闻」的分层与聚合规则（纯函数，无 IO）

## 为什么是排序而不是过滤（用户 2026-08-11）

> 「关于股价，资金流动，龙虎榜这些，明显属于技术分析的，我肯定是放到末尾的，
>   我这个页面主要还是基本面分析为主的」

所以五层**只排序不删除**：major > substantive > news > procedural > technical。

## 为什么公告不进技术面层

公告标题若含「主力资金净流入」等技术面关键词，会被 `_TECHNICAL` 规则命中。但公告
是公司一手披露（如「关于股票主力资金净流入情况的说明公告」），内容本就不涉及交易面
数据，故应返回 substantive 而非 technical。因此技术面判定**只对新闻生效**。
"""

import re

# 大事：用户点进页面最想第一眼看到的
_MAJOR = re.compile(
    r"(业绩预告|业绩快报|业绩预亏|业绩预增"
    r"|减持|增持|回购"
    r"|权益变动|要约收购|收购|重组|合并"
    r"|重大合同|中标|重大资产"
    r"|立案|处罚|诉讼|仲裁"
    r"|解禁|限售股上市流通"
    r"|停牌|复牌|退市风险)"
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
_TECHNICAL = re.compile(
    r"(龙虎榜|主力资金|资金净流入|资金净流出|资金流向|北向资金"
    r"|涨停|跌停|封板|异动|盘中|强势股|大宗交易|融资融券"
    r"|排行|榜单|净流入超|净流出超"
    r"|\d+\s*只股|多股|个股.{0,6}净流)"
)


def classify(item: dict) -> str:
    """返回 major / substantive / news / procedural / technical 之一。

    判定顺序即优先级：大事最先判，避免「XX 证券关于回购的核查意见」被
    程序性规则先截走——那会让一条重磅掉到第 4 层。
    """
    title = str(item.get("title") or "")
    category = str(item.get("category") or "")
    is_announcement = item.get("kind") == "announcement"
    haystack = title + " " + category

    if _MAJOR.search(haystack):
        return "major"
    if _PROCEDURAL.search(haystack):
        return "procedural"
    if is_announcement:
        return "substantive"
    if _TECHNICAL.search(title):
        return "technical"
    return "news"


LAYER_ORDER = ("major", "substantive", "news", "procedural", "technical")

LAYER_LABEL = {
    "major": "重要事项",
    "substantive": "公司公告",
    "news": "相关新闻",
    "procedural": "程序性文件",
    "technical": "交易与资金面",
}
