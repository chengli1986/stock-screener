#!/usr/bin/env python3
"""
update_research_consensus.py — 一致预期(估值分母)自动刷新

背景:`config/research_stocks.json` 里的 consensus 是手工维护的,自各股注册日起从未
更新过(git log -S 验证:旭创 2026-05-03、寒武纪 2026-05-06、茅台 2026-07-07)。而
`update_research_snapshots.py` 每日刷新市值(分子),于是页面上的动态 PE/PS 实际含义是
「今天的价 ÷ 三个月前的预期」——恰恰在回撤行情里最需要准的就是分母。

本脚本为每个注册股票抓取最新机构一致预期,写出 `docs-site/data/{key}-consensus.json`,
并与注册表冻结值对比、对显著变动发出告警。

数据源:同花顺 `ak.stock_profit_forecast_ths`
  - `业绩预测详表-详细指标预测` → 营收/净利,近 3 年实际值 + 未来 3 年预测均值
  - `预测年报净利润` → 机构数 + 最小/均值/最大(离散度,判断均值是否被极端值拉动)
东方财富 `stock_profit_forecast_em` 的 akshare 封装已失效(`RPT_WEB_RESPREDICT` 返回
`result:null`),故不用作备源;港股同花顺不覆盖,由 `--include-hk` 单独走券商个体预测表。

**本脚本不改写 `config/research_stocks.json`** —— 注册表仍是人工权威口径,自动抓取
只落到独立文件供复核。确认无误后再由人工同步注册表。
"""

import argparse
import json
import pathlib
import re
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

BJT = timezone(timedelta(hours=8))

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "config" / "research_stocks.json"
DOCS_DATA = pathlib.Path.home() / "docs-site" / "data"
DEPLOY_DATA = pathlib.Path("/var/www/overview/data")

# 显著变动门槛:超过即在输出里标记并使脚本以告警码退出
DEFAULT_THRESHOLD_PCT = 10.0

_AMOUNT_RE = re.compile(r"^(-?[\d.]+)(亿|万)?$")
_FORECAST_COL_RE = re.compile(r"^预测(\d{4})-平均$")
_ACTUAL_COL_RE = re.compile(r"^(\d{4})-实际值$")

# 详细指标预测表里我们只取这两行,其余(增长率/ROE/每股净资产/市盈率)不是金额
_AMOUNT_ROWS = {"营业收入(元)": "revenue", "净利润(元)": "profit"}


# ── 解析层(纯函数,单元测试覆盖)────────────────────────────────────────────────


def parse_amount(raw: str | None) -> float | None:
    """同花顺金额字符串 → 元。

    `'959.68亿'` → 9.5968e10;`'5629.30万'` → 5.6293e7;`'31.15'`(无单位)→ 原值。
    `'--'` / 空 / 非数字 → None(上游用 `--` 表示该期无预测,不能当 0 处理)。
    """
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    m = _AMOUNT_RE.match(text)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    if unit == "亿":
        return value * 1e8
    if unit == "万":
        return value * 1e4
    return value


def parse_ths_detail(columns: list[str], rows: list[list[str]]) -> dict[str, dict[str, float]]:
    """`业绩预测详表-详细指标预测` → `{'2026E': {'revenue': ..., 'profit': ...}, '2025A': {...}}`。

    预测列形如 `预测2026-平均` → `2026E`;实际值列形如 `2025-实际值` → `2025A`。
    实际值一并返回,便于复核时确认预测起点(如「2026E 营收 960 亿」是不是从
    「2025A 382 亿」跳出来的)。
    """
    col_year: dict[int, str] = {}
    for idx, col in enumerate(columns):
        m = _FORECAST_COL_RE.match(col)
        if m:
            col_year[idx] = f"{m.group(1)}E"
            continue
        m = _ACTUAL_COL_RE.match(col)
        if m:
            col_year[idx] = f"{m.group(1)}A"

    try:
        indicator_idx = columns.index("预测指标")
    except ValueError:
        return {}

    out: dict[str, dict[str, float]] = {}
    for row in rows:
        field = _AMOUNT_ROWS.get(str(row[indicator_idx]).strip())
        if field is None:
            continue
        for idx, year in col_year.items():
            value = parse_amount(row[idx])
            if value is not None:
                out.setdefault(year, {})[field] = value
    return out


def parse_ths_dispersion(columns: list[str], rows: list[list[str]]) -> dict[str, dict]:
    """`预测年报净利润` → `{'2026': {'orgs', 'min', 'mean', 'max', 'spread_ratio'}}`。

    该表数值单位固定为亿元(无后缀),故统一 ×1e8 转元。`spread_ratio = max/min`,
    是复核时最直观的分歧度指标——旭创 2027E 的 max/min 达 2.35 倍,均值的
    参考价值就要打折扣。min ≤ 0 时比值无意义,置 None。
    """
    idx = {name: i for i, name in enumerate(columns)}
    required = ("年度", "预测机构数", "最小值", "均值", "最大值")
    if any(name not in idx for name in required):
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        year = str(row[idx["年度"]]).strip()
        lo = parse_amount(row[idx["最小值"]])
        mean = parse_amount(row[idx["均值"]])
        hi = parse_amount(row[idx["最大值"]])
        if mean is None:
            continue
        rec: dict = {
            "orgs": int(float(row[idx["预测机构数"]])),
            "min": lo * 1e8 if lo is not None else None,
            "mean": mean * 1e8,
            "max": hi * 1e8 if hi is not None else None,
        }
        rec["spread_ratio"] = hi / lo if (lo and hi and lo > 0) else None
        out[year] = rec
    return out


def compare_with_registry(stock: dict, parsed: dict[str, dict[str, float]]) -> list[dict]:
    """把最新一致预期与注册表冻结值逐项对比。

    只对比注册表**已登记**的字段——PS 模式的股票注册表里只有营收,不该凭空生成
    净利对比行。上游缺该期预测时 `latest`/`delta_pct` 均为 None(区别于 0%)。
    """
    field_map = {"profit_yuan": "profit", "revenue_yuan": "revenue"}
    deltas: list[dict] = []
    for year, frozen_fields in sorted(stock.get("consensus", {}).items()):
        for reg_field, parsed_field in field_map.items():
            frozen = frozen_fields.get(reg_field)
            if frozen is None:
                continue
            latest = parsed.get(year, {}).get(parsed_field)
            delta_pct = round((latest / frozen - 1) * 100, 2) if (latest is not None and frozen) else None
            deltas.append(
                {
                    "year": year,
                    "field": parsed_field,
                    "frozen": frozen,
                    "latest": latest,
                    "delta_pct": delta_pct,
                }
            )
    return deltas


def significant_changes(deltas: list[dict], threshold_pct: float = DEFAULT_THRESHOLD_PCT) -> list[dict]:
    """筛出绝对变动 ≥ 门槛的项。下修与上调同等重要,故只看绝对值。"""
    return [
        d for d in deltas
        if d.get("delta_pct") is not None and abs(d["delta_pct"]) >= threshold_pct
    ]


# ── 第三方交叉复核（东方财富 F10，独立于同花顺）────────────────────────────────
#
# 单一数据源无从判断对错，人工 Wind 复核不可持续。东财 F10 ProfitForecast 与同花顺
# 采集口径独立，且多给三样东西：一致预期各字段的机构数、逐家机构明细+发布日期、
# 评级统计。2026-08-03 首次全池实测：40 项比对 39 项差异 <10%。

_EM_F10_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax"
_EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"}
# 6 月内窗口的机构数与同花顺口径最接近，用它做对照
_EM_RATING_WINDOW = "6月内"


def em_secid(symbol: str, exchange: str) -> str:
    """A 股代码 → 东财 F10 格式（`SZ300308`）。港股不走该接口，显式拒绝。

    静默返回一个错误格式会让抓取「成功但为空」，比直接报错更难排查。
    """
    if exchange not in ("SH", "SZ"):
        raise ValueError(f"东财 F10 ProfitForecast 不支持 exchange={exchange}（{symbol}）")
    return f"{exchange}{symbol}"


def parse_em_estimates(yctj_list: list[dict]) -> dict[str, dict]:
    """`yctj_list` → `{'2026E': {'profit_yuan', 'revenue_yuan', 'profit_orgs', 'revenue_orgs'}}`。

    `YEAR_MARK == 'A'` 是已披露实际值，必须剔除，否则会把历史当预测参与比对。
    """
    out: dict[str, dict] = {}
    for row in yctj_list or []:
        if str(row.get("YEAR_MARK")) != "E":
            continue
        year = f"{row.get('YEAR')}E"
        out[year] = {
            "profit_yuan": _round_yuan(row.get("PARENT_NETPROFIT")),
            "revenue_yuan": _round_yuan(row.get("TOTAL_OPERATE_INCOME")),
            "profit_orgs": row.get("PARENT_NETPROFIT_COUNT"),
            "revenue_orgs": row.get("TOTAL_OPERATE_INCOME_COUNT"),
        }
    return out


def parse_em_ratings(pjtj: list[dict]) -> dict | None:
    """`pjtj` → 6 月内窗口的评级分布。取不到该窗口返回 None（不退化到别的窗口）。"""
    for row in pjtj or []:
        if row.get("DATE_TYPE") != _EM_RATING_WINDOW:
            continue
        return {
            "window": _EM_RATING_WINDOW,
            "orgs": row.get("RATING_ORG_NUM"),
            "rating": row.get("COMPRE_RATING"),
            "rating_score": row.get("COMPRE_RATING_NUM"),
            "buy": row.get("RATING_BUY_NUM"),
            "add": row.get("RATING_ADD_NUM"),
            "neutral": row.get("RATING_NEUTRAL_NUM"),
            "reduce": row.get("RATING_REDUCE_NUM"),
            "sale": row.get("RATING_SALE_NUM"),
        }
    return None


def parse_em_brokers(ycmx: list[dict], limit: int = 20) -> list[dict]:
    """`ycmx` → 逐家机构明细（含发布日期）。发布日期用于识别陈旧预测。"""
    out = []
    for row in (ycmx or [])[:limit]:
        out.append({
            "org": row.get("ORG_NAME_ABBR"),
            "published": (row.get("PUBLISH_DATE") or "")[:10],
            "profit_y2_yuan": _round_yuan(row.get("PARENT_NETPROFIT2")),
            "profit_y3_yuan": _round_yuan(row.get("PARENT_NETPROFIT3")),
        })
    return out


def cross_check(ths_est: dict, em_est: dict, threshold_pct: float = DEFAULT_THRESHOLD_PCT) -> dict:
    """逐年逐字段比对两源，给出 CONFIRMED / DIVERGENT / UNVERIFIED。

    **UNVERIFIED 与 CONFIRMED 必须区分开** —— 拿不到第二源（港股、覆盖过薄）时
    默默当作通过，等于把「没验证」伪装成「已验证」，正是本模块要消灭的问题。
    只比对主源（同花顺）已给出的字段：PS 模式标的注册表里没有净利，不该凭空生成比对行。
    """
    field_map = (("profit", "profit_yuan"), ("revenue", "revenue_yuan"))
    out: dict[str, dict] = {}
    for year, ths_fields in sorted((ths_est or {}).items()):
        em_fields = (em_est or {}).get(year) or {}
        year_out: dict[str, dict] = {}
        for name, key in field_map:
            primary = ths_fields.get(key)
            if primary in (None, 0):
                continue
            secondary = em_fields.get(key)
            if secondary in (None, 0):
                year_out[name] = {"primary": primary, "secondary": None,
                                  "diff_pct": None, "verdict": "UNVERIFIED"}
                continue
            diff = round((secondary / primary - 1) * 100, 2)
            year_out[name] = {
                "primary": primary,
                "secondary": secondary,
                "diff_pct": diff,
                "verdict": "DIVERGENT" if abs(diff) >= threshold_pct else "CONFIRMED",
            }
        if year_out:
            out[year] = year_out
    return out


def cross_check_summary(checks: dict) -> dict[str, int]:
    """裁决计数，供控制台与告警使用。"""
    tally: dict[str, int] = {}
    for year_out in (checks or {}).values():
        for item in year_out.values():
            v = item["verdict"]
            tally[v] = tally.get(v, 0) + 1
    return tally


# 中位数最小样本量：低于此值不给中位数（长鑫 2 家 / 长光华芯 3 家覆盖，算了也是假精度）
MIN_MEDIAN_SAMPLES: int = 5


def parse_em_broker_estimates(ycmx: list[dict] | None) -> dict[str, list[float]]:
    """`ycmx` 逐家明细 → `{'2026E': [各家归母净利…]}`。

    年份轴按 `YEAR_MARK` **动态**映射，不能写死 `YEAR2=2026`——不同标的、
    跨年后位次都会变。`'A'` 是已披露实际值，剔除。
    """
    out: dict[str, list[float]] = {}
    for row in ycmx or []:
        for i in (1, 2, 3, 4):
            if str(row.get(f"YEAR_MARK{i}")) != "E":
                continue
            year = row.get(f"YEAR{i}")
            value = row.get(f"PARENT_NETPROFIT{i}")
            if year is None or value is None:
                continue
            out.setdefault(f"{year}E", []).append(float(value))
    return out


def broker_stats(per_year: dict[str, list[float]],
                 min_samples: int = MIN_MEDIAN_SAMPLES) -> dict[str, dict]:
    """逐家预测 → 中位数/均值/极值/样本数。

    **样本不足时不给中位数** —— 长鑫仅 2 家、长光华芯 3 家覆盖，
    对这类标的算中位数是假精度。留 None 并置 `insufficient_samples`，
    与 cross_check 里 UNVERIFIED 的处理哲学一致：说不知道，好过给个看起来很准的数。
    min/max 即便样本少也仍是真信息，照常给出。
    """
    out: dict[str, dict] = {}
    for year, values in sorted((per_year or {}).items()):
        vals = sorted(v for v in values if v is not None)
        if not vals:
            continue
        n = len(vals)
        enough = n >= min_samples
        mid = n // 2
        median = (vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2) if enough else None
        out[year] = {
            "median": _round_yuan(median),
            "mean": _round_yuan(sum(vals) / n),
            "min": _round_yuan(vals[0]),
            "max": _round_yuan(vals[-1]),
            "count": n,
            "insufficient_samples": not enough,
        }
    return out


# ── 港股一致预期（yfinance）────────────────────────────────────────────────────
#
# 分工：A 股走同花顺 + 东财 F10；**港股全部走 yfinance**。
# 同花顺不覆盖港股、东财 F10 ProfitForecast 仅支持 A 股、Longbridge 账户无基本面
# 数据权限（实测连腾讯 0700.HK 的 financial-report 都返回空）——yfinance 是实测唯一可用源。

_YF_TIMEOUT_S = 45


def yf_ticker(symbol: str) -> str:
    """港股代码 → yfinance 格式（4 位零填充 + `.HK`）。

    实测：`2513.HK` ✓ / `02513.HK` ✗；`0700.HK` ✓ / `700.HK` ✗。
    注册表里智谱存的是 5 位的 `02513`，必须归一。
    """
    digits = str(symbol).strip()
    if not digits.isdigit():
        raise ValueError(f"港股代码应为纯数字，得到 {symbol!r}")
    return f"{int(digits):04d}.HK"


def resolve_forecast_years(next_fiscal_year_end) -> dict[str, str]:
    """`nextFiscalYearEnd` 时间戳 → `{'0y': '2026E', '+1y': '2027E'}`。

    yfinance 的 `0y` / `+1y` 是「当前财年 / 下一财年」这种**相对期**，跨年会自动平移。
    直接写死年份会在跨年后错位，故用财年末锚定实际年份。
    **拿不到锚点就返回空** —— 宁可没有预测，也不能把年份猜错后写进估值分母。
    """
    if not next_fiscal_year_end:
        return {}
    try:
        year = datetime.fromtimestamp(int(next_fiscal_year_end), timezone.utc).year
    except (ValueError, TypeError, OSError):
        return {}
    return {"0y": f"{year}E", "+1y": f"{year + 1}E"}


def parse_yf_estimates(revenue_est, earnings_est, next_fiscal_year_end) -> dict[str, dict]:
    """yfinance 预测 → 与 `parse_em_estimates` **同构**的 estimates。

    字段名刻意与 A 股路径一致，页面水合逻辑无需分市场处理。
    只取年度期（`0y`/`+1y`），季度期 `0q`/`+1q` 不进年度估值分母。
    yfinance 对无覆盖期返回 0，不能当成「预测为零」——一律省略该字段。
    """
    years = resolve_forecast_years(next_fiscal_year_end)
    out: dict[str, dict] = {}
    for period, label in years.items():
        rec: dict = {}
        r = (revenue_est or {}).get(period) or {}
        avg = r.get("avg")
        if avg:  # 0 视同缺失
            rec["revenue_yuan"] = _round_yuan(avg)
            rec["revenue_orgs"] = int(r["numberOfAnalysts"]) if r.get("numberOfAnalysts") else None
            if r.get("low"):
                rec["revenue_min_yuan"] = _round_yuan(r["low"])
            if r.get("high"):
                rec["revenue_max_yuan"] = _round_yuan(r["high"])
        e = (earnings_est or {}).get(period) or {}
        eps = e.get("avg")
        # EPS 为负是合法值（智谱在亏损），不能用真值判断丢掉
        if eps is not None and eps == eps:  # 排除 NaN
            rec["eps"] = eps
            rec["eps_orgs"] = int(e["numberOfAnalysts"]) if e.get("numberOfAnalysts") else None
        if rec:
            out[label] = rec
    return out


def parse_yf_ratings(recommendations, price_targets) -> dict | None:
    """yfinance 评级分布 + 目标价 → 与 `parse_em_ratings` 同构。"""
    rows = list(recommendations or [])
    if not rows:
        return None
    cur = rows[0]
    buckets = {k: int(cur.get(k) or 0) for k in
               ("strongBuy", "buy", "hold", "sell", "strongSell")}
    pt = price_targets or {}
    return {
        "window": cur.get("period") or "0m",
        "orgs": sum(buckets.values()),
        "buy": buckets["buy"],
        "strong_buy": buckets["strongBuy"],
        "hold": buckets["hold"],
        "sell": buckets["sell"],
        "strong_sell": buckets["strongSell"],
        "target_mean": pt.get("mean"),
        "target_median": pt.get("median"),
        "target_high": pt.get("high"),
        "target_low": pt.get("low"),
    }


# ── 港股逐家机构明细（经济通 ETNet）────────────────────────────────────────────
#
# 港股的一致预期验证只有**一层**能做：同一源内的多机构对比。
# 跨源验证做不了——yfinance 给营收+EPS、ET 给净利，没有任何指标两源都有；
# 要打通得除以股本，而股本是不确定量，会制造假的 CONFIRMED/DIVERGENT。
#
# 目标价与评级**只落盘展示、不参与判定**：目标价 = 盈利预测 × 假设倍数，是复合量，
# 且两源底层券商报告重叠、一致有部分来自共享输入；评级是压缩的序数且严重偏向买入。
#
# 单位由 ET「去年度业绩表现」直接证实：`集团纯利 -4,698.20 百万元人民币`。
# `每股盈利`（单位「分」）弃用——各家对未来股本假设不同，数值不可比。

_ET_TIMEOUT_S = 60
_ET_COLS = ("财政年度", "纯利/亏损", "证券商", "更新日期")


def parse_et_brokers(columns: list[str], rows: list[list]) -> dict[str, list[dict]]:
    """经济通「盈利预测概览」→ `{'2026E': [{'org','published','value'}]}`（value 单位：元）。"""
    idx = {c: i for i, c in enumerate(columns)}
    if any(c not in idx for c in _ET_COLS):
        return {}

    out: dict[str, list[dict]] = {}
    for row in rows:
        year = str(row[idx["财政年度"]]).strip()
        if not year.isdigit():
            continue
        raw = str(row[idx["纯利/亏损"]]).strip()
        try:
            profit_mn = float(raw)
        except ValueError:
            continue  # 空值/'nan' → 该家未给该年预测
        if raw.lower() in ("nan", ""):
            continue
        out.setdefault(f"{year}E", []).append({
            "org": str(row[idx["证券商"]]).strip(),
            "published": str(row[idx["更新日期"]]).strip()[:10],
            "value": profit_mn * 1e6,          # 百万元 → 元
        })
    return out


def fetch_et(symbol: str) -> dict:
    """拉经济通逐家明细 + 目标价（展示用）。ET 实测会瞬时挂起，必须硬超时。"""
    def _work():
        import akshare as ak

        df = ak.stock_hk_profit_forecast_et(symbol=symbol, indicator="盈利预测概览")
        if df is None or df.empty:
            return {"brokers": {}, "targets": None}
        cols = df.columns.tolist()
        rows = df.astype(str).values.tolist()
        brokers = parse_et_brokers(cols, rows)
        targets = None
        if "目标价" in cols:
            tg = [float(v) for v in df["目标价"] if v == v]
            if tg:
                st = sorted(tg)
                m = len(st) // 2
                targets = {
                    "count": len(st),
                    "median": round(st[m] if len(st) % 2 else (st[m - 1] + st[m]) / 2, 2),
                    "mean": round(sum(st) / len(st), 2),
                    "min": st[0],
                    "max": st[-1],
                }
        return {"brokers": brokers, "targets": targets}

    return call_with_timeout(_work, _ET_TIMEOUT_S, label=f"etnet[{symbol}]")


def fetch_yf(symbol: str) -> dict:
    """拉 yfinance 港股一致预期。加硬超时——yfinance 爬 Yahoo，会被限流/改版卡住。"""
    import yfinance as yf

    ticker = yf_ticker(symbol)

    def _work():
        t = yf.Ticker(ticker)
        info = t.info
        rev = t.revenue_estimate
        earn = t.earnings_estimate
        to_dict = lambda df: (  # noqa: E731
            {str(i): {k: v for k, v in row.items()} for i, row in df.to_dict("index").items()}
            if df is not None and not df.empty else {}
        )
        recs = t.recommendations
        return {
            "estimates": parse_yf_estimates(to_dict(rev), to_dict(earn),
                                            info.get("nextFiscalYearEnd")),
            "ratings": parse_yf_ratings(
                recs.to_dict("records") if recs is not None and not recs.empty else [],
                t.analyst_price_targets),
            "financial_currency": info.get("financialCurrency"),
            "quote_currency": info.get("currency"),
        }

    return call_with_timeout(_work, _YF_TIMEOUT_S, label=f"yfinance[{ticker}]")


# ── 稳健统计：新鲜度加权 + MAD 离群标记 ────────────────────────────────────────
#
# 分析师预测不满足正态假设：每个人锚定同一份公司指引、看得到彼此的数、且职业风险
# 不对称（跟随共识错了没事，独立错了要走人）→ 实际形态是「紧密聚类 + 少数离群」。
# 拟合正态会把 herding 造成的假紧密当成高置信度，同时给离群值不该有的权重。
# 故不拟合分布，只用不依赖分布假设的稳健量。

# 半衰期：一个季度。周期股尤其如此——四个月前的预测早已被新数据推翻。
RECENCY_HALF_LIFE_DAYS = 90
# 日期缺失时的权重。不是 0（缺日期≠数据无效），也不是 1（不该白拿满权重）。
UNKNOWN_AGE_WEIGHT = 0.5
# 离群门槛：|x − median| > k × MAD。用原始 MAD，不做 1.4826 正态化缩放——
# 那个系数的意义正是「等价于正态下的 σ」，而我们恰恰不假设正态。
MAD_OUTLIER_K = 3.0
# 经济门槛：偏离中位数须达到该百分比才算离群。首跑实测（2026-08-04）发现单用 MAD 会
# 误标——MAD 衡量的是相对**该标的自身共识紧密度**的异常，共识越紧、同样绝对偏离的
# MAD 倍数越大。茅台 46 家高度一致，3.3×MAD 只对应 −5% 偏离也被标；而寒武纪
# 7×MAD 对应 +43%。统计上异常 ≠ 经济上重要，故设双门槛。
MIN_DEVIATION_PCT = 20.0


def estimate_age_days(published: str | None, today) -> int | None:
    """发布日期 → 距今天数。无法解析返回 None（区别于 0）。"""
    if not published:
        return None
    try:
        d = datetime.strptime(str(published)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (today - d).days


def recency_weight(age_days: int | None, half_life_days: int = RECENCY_HALF_LIFE_DAYS) -> float:
    """指数衰减权重。半衰期默认 90 天（一个季度）。"""
    if age_days is None:
        return UNKNOWN_AGE_WEIGHT
    return 0.5 ** (max(age_days, 0) / half_life_days)


def median_abs_deviation(values: list[float]) -> float:
    """MAD = median(|x − median|)。不做正态化缩放，理由见本节顶部注释。"""
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    med = vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2
    devs = sorted(abs(v - med) for v in vals)
    m = len(devs) // 2
    return devs[m] if len(devs) % 2 else (devs[m - 1] + devs[m]) / 2


def flag_outliers(values: list[float], k: float = MAD_OUTLIER_K,
                  min_deviation_pct: float = MIN_DEVIATION_PCT) -> list[bool]:
    """逐点标记是否离群。**标记而不删除** —— 离群者可能是唯一看对的人。

    **双门槛**：既要统计上异常（|x−median| > k×MAD），又要经济上重要
    （偏离中位数 ≥ min_deviation_pct）。只用前者会在共识紧密的标的上大量误标
    （茅台 3.3×MAD 仅对应 −5%），只用后者会在本就分歧大的标的上大量误标。

    n<3 时中位数与 MAD 都没有意义，一律不标。MAD==0 时不能除零，也一律不标。
    """
    n = len(values)
    if n < 3:
        return [False] * n
    mad = median_abs_deviation(values)
    if mad <= 0:
        return [False] * n
    vals = sorted(values)
    mid = n // 2
    med = vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2
    if med == 0:
        return [False] * n
    return [
        abs(v - med) > k * mad and abs(v / med - 1) * 100 >= min_deviation_pct
        for v in values
    ]


def parse_em_broker_records(ycmx: list[dict] | None) -> dict[str, list[dict]]:
    """`ycmx` → `{'2026E': [{'org','published','value'}]}`，保留机构名与发布日期。

    年份轴按 `YEAR_MARK` 动态映射（跨年与不同标的位次会变）。
    """
    out: dict[str, list[dict]] = {}
    for row in ycmx or []:
        org = row.get("ORG_NAME_ABBR")
        pub = (row.get("PUBLISH_DATE") or "")[:10]
        for i in (1, 2, 3, 4):
            if str(row.get(f"YEAR_MARK{i}")) != "E":
                continue
            year, value = row.get(f"YEAR{i}"), row.get(f"PARENT_NETPROFIT{i}")
            if year is None or value is None:
                continue
            out.setdefault(f"{year}E", []).append(
                {"org": org, "published": pub, "value": float(value)}
            )
    return out


def robust_stats(per_year: dict[str, list[dict]], today=None,
                 half_life_days: int = RECENCY_HALF_LIFE_DAYS,
                 k: float = MAD_OUTLIER_K,
                 min_samples: int = MIN_MEDIAN_SAMPLES) -> dict[str, dict]:
    """逐家预测记录 → 稳健统计量。

    在既有 median/mean/min/max 之上增加：
    - `weighted_mean`：按发布日期指数衰减加权（旭创最老一份已 124 天，不该等权）
    - `outliers`：MAD 标记出的离群机构（含名字与偏离倍数，便于人工判断）
    - `oldest_age_days` / `newest_age_days`：这组预期整体还新不新
    """
    today = today or datetime.now(BJT).date()
    out: dict[str, dict] = {}
    for year, records in sorted((per_year or {}).items()):
        recs = [r for r in records if r.get("value") is not None]
        if not recs:
            continue
        values = [r["value"] for r in recs]
        ages = [estimate_age_days(r.get("published"), today) for r in recs]
        weights = [recency_weight(a, half_life_days) for a in ages]
        wsum = sum(weights)

        n = len(values)
        enough = n >= min_samples
        svals = sorted(values)
        mid = n // 2
        median = (svals[mid] if n % 2 else (svals[mid - 1] + svals[mid]) / 2) if enough else None

        # 薄覆盖时显式回退到简单算术平均：2-3 家覆盖下中位数/加权/离群检测全无意义，
        # 与其让下游猜该用哪个统计量，不如直接把结论和理由一起落盘。
        simple_mean = sum(values) / n
        if enough:
            preferred_stat, preferred_value, preferred_reason = "median", median, "robust"
        else:
            preferred_stat, preferred_value, preferred_reason = "mean", simple_mean, "thin_coverage"

        flags = flag_outliers(values, k)
        mad = median_abs_deviation(values) if n >= 3 else 0.0
        med_for_dev = svals[mid] if n % 2 else (svals[mid - 1] + svals[mid]) / 2
        outliers = [
            {
                "org": recs[i].get("org"),
                "published": recs[i].get("published"),
                "value": _round_yuan(values[i]),
                "mad_multiple": round(abs(values[i] - med_for_dev) / mad, 1) if mad > 0 else None,
                "deviation_pct": round((values[i] / med_for_dev - 1) * 100, 1) if med_for_dev else None,
            }
            for i, is_out in enumerate(flags) if is_out
        ]
        known_ages = [a for a in ages if a is not None]

        out[year] = {
            "preferred_stat": preferred_stat,
            "preferred_value": _round_yuan(preferred_value),
            "preferred_reason": preferred_reason,
            "median": _round_yuan(median),
            "mean": _round_yuan(simple_mean),
            "weighted_mean": _round_yuan(
                sum(v * w for v, w in zip(values, weights)) / wsum) if wsum > 0 else None,
            "min": _round_yuan(svals[0]),
            "max": _round_yuan(svals[-1]),
            "count": n,
            "insufficient_samples": not enough,
            "mad": _round_yuan(mad) if n >= 3 else None,
            "outliers": outliers,
            "oldest_age_days": max(known_ages) if known_ages else None,
            "newest_age_days": min(known_ages) if known_ages else None,
        }
    return out


# ── 背离告警邮件 ───────────────────────────────────────────────────────────────

_ENV_FILE = pathlib.Path.home() / ".stock-monitor.env"


def build_divergence_alert_html(rows: list[dict], fetched_at: str) -> str | None:
    """两源背离 → 告警邮件正文。无背离返回 None（不发空邮件）。"""
    if not rows:
        return None
    import html as _html

    trs = []
    for r in rows:
        label = "净利" if r["field"] == "profit" else "营收"
        trs.append(
            "<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'>{_html.escape(str(r['name']))}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'>{_html.escape(str(r['year']))}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'>{label}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right'>{r['primary'] / 1e8:.1f} 亿</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right'>{r['secondary'] / 1e8:.1f} 亿</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right;color:#c00'>{r['diff_pct']:+.1f}%</td>"
            "</tr>"
        )
    return (
        "<html><body style='font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;color:#222'>"
        "<h2 style='color:#c00;margin:0 0 6px'>一致预期两源背离告警</h2>"
        f"<p style='margin:0 0 14px;color:#666'>抓取时间 {_html.escape(fetched_at)}　·　"
        "主源 同花顺　vs　交叉源 东方财富 F10</p>"
        "<table style='border-collapse:collapse;font-size:13px'>"
        "<tr style='background:#f5f5f5'>"
        "<th style='padding:6px 10px;text-align:left'>标的</th>"
        "<th style='padding:6px 10px;text-align:left'>年度</th>"
        "<th style='padding:6px 10px;text-align:left'>口径</th>"
        "<th style='padding:6px 10px;text-align:right'>同花顺</th>"
        "<th style='padding:6px 10px;text-align:right'>东财</th>"
        "<th style='padding:6px 10px;text-align:right'>差异</th>"
        "</tr>" + "".join(trs) + "</table>"
        "<p style='margin:16px 0 0;color:#666;font-size:12px'>"
        "两源差异超过门槛，说明该标的的一致预期本身分歧较大或某一源口径异常，"
        "使用其 PE/PEG 时请留意。本邮件由 <code>update_research_consensus.py</code> 自动发出。</p>"
        "</body></html>"
    )


def _load_env() -> dict:
    env: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"')
    return env


def send_divergence_alert(html_body: str, subject: str) -> None:
    """走与 research_data_health.py 同款的 163 SMTP_SSL 通道。"""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    env = _load_env()
    if not env.get("SMTP_USER") or not env.get("SMTP_PASS"):
        print("WARN: 未找到 SMTP 凭据，跳过告警邮件", file=sys.stderr)
        return
    to_addr = env.get("MAIL_TO", env["SMTP_USER"])
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = env["SMTP_USER"]
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL(env.get("SMTP_SERVER", "smtp.163.com"),
                          int(env.get("SMTP_PORT", "465")), timeout=30) as s:
        s.login(env["SMTP_USER"], env["SMTP_PASS"])
        s.sendmail(env["SMTP_USER"], [to_addr], msg.as_string())
    print(f"  背离告警邮件已发送 → {to_addr}", flush=True)


def fetch_em(symbol: str, exchange: str) -> dict:
    """拉东财 F10 盈利预测。返回 `{estimates, ratings, brokers}`。"""
    r = requests.get(_EM_F10_URL, params={"code": em_secid(symbol, exchange)},
                     timeout=25, headers=_EM_HEADERS)
    r.raise_for_status()
    d = r.json()
    ycmx = d.get("ycmx")
    return {
        "estimates": parse_em_estimates(d.get("yctj_list")),
        "ratings": parse_em_ratings(d.get("pjtj")),
        "brokers": parse_em_brokers(ycmx),
        "broker_stats": robust_stats(parse_em_broker_records(ycmx)),
    }


def _round_yuan(value: float | None) -> int | None:
    """`亿 → 元` 的 ×1e8 会留浮点尾巴(233407000000.00003),金额一律取整到元。"""
    return round(value) if value is not None else None


def build_record(
    stock: dict,
    parsed: dict[str, dict[str, float]],
    dispersion: dict[str, dict],
    fetched_at: str,
    source: str = "ths",
) -> dict:
    """组装落盘 JSON。

    必须带 `source` + `fetched_at` —— 这份文件存在的全部理由就是「注册表变成了化石
    而没人看得出来」,如果它自己不带抓取时间,一年后就会重演同一个问题。
    """
    estimates: dict[str, dict] = {}
    for year, fields in sorted(parsed.items()):
        rec: dict = {}
        if "revenue" in fields:
            rec["revenue_yuan"] = _round_yuan(fields["revenue"])
        if "profit" in fields:
            rec["profit_yuan"] = _round_yuan(fields["profit"])
        disp = dispersion.get(year[:4]) if year.endswith("E") else None
        if disp:
            rec["orgs"] = disp["orgs"]
            rec["profit_min_yuan"] = _round_yuan(disp["min"])
            rec["profit_max_yuan"] = _round_yuan(disp["max"])
            rec["spread_ratio"] = round(disp["spread_ratio"], 2) if disp["spread_ratio"] else None
        estimates[year] = rec

    return {
        "symbol": stock["symbol"],
        "name": stock["name"],
        "source": source,
        "fetched_at": fetched_at,
        "estimates": estimates,
        "deltas_vs_registry": compare_with_registry(stock, parsed),
    }


# ── 抓取层(I/O)────────────────────────────────────────────────────────────────


_THS_TIMEOUT_S = 45


def call_with_timeout(fn, timeout_s: int, label: str = ""):
    """给没有 timeout 参数的调用施加硬性上限（daemon 线程 + join）。

    akshare `stock_profit_forecast_ths` 内部是 `requests.get(url, headers=headers)`，
    **不带 timeout**——2026-08-04 实测在贵州茅台上挂起 9.5 分钟不返回，
    会让 cron 跑满 900s 被 SIGKILL、剩余标的全部无数据。
    与 `phase0_spike._fetch_csi_once`（`8ca09ad`）同款守卫。

    worker 抛出的真实异常原样传播，不伪装成超时——否则排查时会误判上游状态。
    """
    import threading

    box: dict = {}

    def _worker() -> None:
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001 —— 需原样带回主线程
            box["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"{label or 'call'} 超过 {timeout_s}s 未返回（上游疑似挂起）")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def fetch_ths(symbol: str) -> tuple[dict, dict]:
    """拉同花顺两张表,返回 `(parsed, dispersion)`。akshare 在函数内 import 以便测试免装。"""
    import akshare as ak

    detail = call_with_timeout(
        lambda: ak.stock_profit_forecast_ths(symbol=symbol, indicator="业绩预测详表-详细指标预测"),
        _THS_TIMEOUT_S, label=f"ths-detail[{symbol}]")
    if detail is None or detail.empty:
        raise ValueError("同花顺无业绩预测详表(可能本年度暂无机构预测)")
    parsed = parse_ths_detail(detail.columns.tolist(), detail.astype(str).values.tolist())
    if not parsed:
        raise ValueError("详细指标预测表解析出 0 条金额(上游表结构可能已变)")

    try:
        prof = call_with_timeout(
            lambda: ak.stock_profit_forecast_ths(symbol=symbol, indicator="预测年报净利润"),
            _THS_TIMEOUT_S, label=f"ths-dispersion[{symbol}]")
        dispersion = (
            parse_ths_dispersion(prof.columns.tolist(), prof.astype(str).values.tolist())
            if prof is not None and not prof.empty
            else {}
        )
    except Exception as e:  # 离散度是加分项,拿不到不该让整只股票失败
        print(f"WARN: [{symbol}] 离散度表获取失败({type(e).__name__}: {e}),仅落均值", file=sys.stderr)
        dispersion = {}

    return parsed, dispersion


def write_and_deploy(snapshot_key: str, record: dict) -> None:
    """写 docs-site/data/ 并同步到 /var/www(与 update_research_snapshots.py 同款)。"""
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    path = DOCS_DATA / f"{snapshot_key}-consensus.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if DEPLOY_DATA.is_dir():
        shutil.copy2(path, DEPLOY_DATA / path.name)


def _fmt_yi(value: float | None) -> str:
    return f"{value / 1e8:.1f}亿" if value is not None else "—"


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取研究股票最新机构一致预期")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PCT,
                    help=f"显著变动门槛百分比(默认 {DEFAULT_THRESHOLD_PCT})")
    ap.add_argument("--dry-run", action="store_true", help="只打印对比,不写任何文件")
    ap.add_argument("--symbol", help="只跑单只股票(调试用)")
    ap.add_argument("--no-email", action="store_true", help="有背离也不发告警邮件")
    args = ap.parse_args()

    stocks = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if args.symbol:
        stocks = [s for s in stocks if s["symbol"] == args.symbol]
        if not stocks:
            print(f"ERROR: 注册表中没有 {args.symbol}", file=sys.stderr)
            return 1

    fetched_at = datetime.now(BJT).isoformat(timespec="seconds")
    errors: list[str] = []
    flagged_all: list[tuple[str, dict]] = []
    divergent_all: list[tuple] = []
    skipped_hk: list[str] = []

    for stock in stocks:
        symbol, name = stock["symbol"], stock["name"]
        if stock.get("exchange") == "HK":
            # 港股走 yfinance（2026-08-04 起）。同花顺不覆盖港股、东财 F10 仅支持 A 股、
            # Longbridge 账户无基本面权限——yfinance 是实测唯一可用源。
            # 产出结构与 A 股同构，页面水合逻辑无需分市场处理；
            # 但只有单源，cross_check 一律 UNVERIFIED（待第 3 步接 akshare ET 交叉）。
            try:
                yf_data = fetch_yf(symbol)
                est = yf_data["estimates"]
                if not est:
                    raise ValueError("yfinance 返回空预测（财年锚点或覆盖缺失）")
                record = {
                    "symbol": symbol,
                    "name": name,
                    "source": "yfinance",
                    "fetched_at": fetched_at,
                    "estimates": est,
                    "deltas_vs_registry": compare_with_registry(
                        stock,
                        {y: {"revenue": v.get("revenue_yuan")} for y, v in est.items()},
                    ),
                    "cross_check": cross_check(
                        {y: {"revenue_yuan": v.get("revenue_yuan")} for y, v in est.items()},
                        {}, args.threshold,
                    ),
                    "cross_source": None,
                    "ratings": yf_data.get("ratings"),
                    "financial_currency": yf_data.get("financial_currency"),
                    "quote_currency": yf_data.get("quote_currency"),
                }
                record["cross_check_summary"] = cross_check_summary(record["cross_check"])

                # 经济通逐家明细：港股唯一能做的验证层（同源多机构对比）。
                # 拿不到不该让整只失败——主源数据仍有效，只是少了逐家统计。
                try:
                    et = fetch_et(symbol)
                    record["broker_stats"] = robust_stats(et["brokers"])
                    record["broker_source"] = "etnet"
                    # 目标价只展示、不参与任何判定（复合量，且与 yfinance 券商池重叠）
                    record["target_price_display_only"] = {
                        "etnet": et.get("targets"),
                        "yfinance": (record.get("ratings") or {}).get("target_median"),
                    }
                except Exception as e:
                    print(f"WARN: [{symbol}] 经济通逐家明细不可用（{type(e).__name__}: {e}）",
                          file=sys.stderr)
                    record["broker_stats"] = {}
                    record["broker_source"] = None

                if not args.dry_run:
                    write_and_deploy(stock["snapshot_key"], record)
                flagged = significant_changes(record["deltas_vs_registry"], args.threshold)
                flagged_all.extend((name, d) for d in flagged)
                bs0 = (record.get("broker_stats") or {}).get(next(iter(est), ""), {})
                e0 = next(iter(est.values()), {})
                print(
                    f"  [{symbol}] ✓ {name}（港股/yfinance）"
                    f"  {next(iter(est), '—')} 营收{_fmt_yi(e0.get('revenue_yuan'))}"
                    f"  机构{e0.get('revenue_orgs') or '—'}家"
                    f"  币种{yf_data.get('financial_currency')}"
                    f"  逐家{bs0.get('count') or '—'}家(经济通)"
                    f"{'  ⚠' + str(len(flagged)) + '项显著变动' if flagged else ''}"
                )
            except Exception as e:
                msg = f"[{symbol}] {name} FAILED(yfinance): {type(e).__name__}: {e}"
                print(f"ERROR: {msg}", file=sys.stderr)
                errors.append(msg)
            time.sleep(1.0)
            continue

        try:
            parsed, dispersion = fetch_ths(symbol)
            record = build_record(stock, parsed, dispersion, fetched_at)

            # 第三方交叉复核：东财 F10 拿不到不该让整只股票失败——
            # 主源数据仍然有效，只是这次没被验证，如实标 UNVERIFIED。
            em = None
            try:
                em = fetch_em(symbol, stock["exchange"])
            except Exception as e:
                print(f"WARN: [{symbol}] 东财交叉源不可用（{type(e).__name__}: {e}），本次标记为未验证",
                      file=sys.stderr)
            record["cross_check"] = cross_check(
                {y: {"profit_yuan": v.get("profit_yuan"), "revenue_yuan": v.get("revenue_yuan")}
                 for y, v in record["estimates"].items() if y.endswith("E")},
                (em or {}).get("estimates") or {},
                args.threshold,
            )
            record["cross_check_summary"] = cross_check_summary(record["cross_check"])
            record["cross_source"] = "eastmoney_f10" if em else None
            if em:
                record["ratings"] = em.get("ratings")
                record["brokers"] = em.get("brokers")
                record["broker_stats"] = em.get("broker_stats")

            if not args.dry_run:
                write_and_deploy(stock["snapshot_key"], record)

            flagged = significant_changes(record["deltas_vs_registry"], args.threshold)
            flagged_all.extend((name, d) for d in flagged)
            for year, fields in record["cross_check"].items():
                for fname, item in fields.items():
                    if item["verdict"] == "DIVERGENT":
                        divergent_all.append((name, year, fname, item))

            e26 = record["estimates"].get("2026E", {})
            orgs = e26.get("orgs")
            cs = record["cross_check_summary"]
            xc = f"  交叉[确认{cs.get('CONFIRMED', 0)}/背离{cs.get('DIVERGENT', 0)}/未验{cs.get('UNVERIFIED', 0)}]"
            print(
                f"  [{symbol}] ✓ {name}  2026E 营收{_fmt_yi(e26.get('revenue_yuan'))} "
                f"净利{_fmt_yi(e26.get('profit_yuan'))}  机构{orgs if orgs else '—'}家{xc}"
                f"{'  ⚠' + str(len(flagged)) + '项显著变动' if flagged else ''}"
            )
        except Exception as e:
            msg = f"[{symbol}] {name} FAILED: {type(e).__name__}: {e}"
            print(f"ERROR: {msg}", file=sys.stderr)
            errors.append(msg)
        time.sleep(1.0)  # 同花顺无官方限频,保守间隔

    if skipped_hk:
        print(f"\n跳过港股(同花顺不覆盖,人工维护): {', '.join(skipped_hk)}")

    if divergent_all:
        print(f"\n=== 两源背离（|Δ| ≥ {args.threshold}%，同花顺 vs 东财 F10）===")
        for name, year, fname, it in divergent_all:
            label = "净利" if fname == "profit" else "营收"
            print(f"  {name} {year} {label}: 同花顺 {_fmt_yi(it['primary'])} / "
                  f"东财 {_fmt_yi(it['secondary'])}  ({it['diff_pct']:+.1f}%)")

    if flagged_all:
        print(f"\n=== 显著变动(|Δ| ≥ {args.threshold}%),需人工复核后同步注册表 ===")
        for name, d in flagged_all:
            label = "净利" if d["field"] == "profit" else "营收"
            print(
                f"  {name} {d['year']} {label}: {_fmt_yi(d['frozen'])} → "
                f"{_fmt_yi(d['latest'])}  ({d['delta_pct']:+.1f}%)"
            )

    if divergent_all and not args.dry_run and not args.no_email:
        html = build_divergence_alert_html(
            [{"name": n, "year": y, "field": f, **it} for n, y, f, it in divergent_all],
            fetched_at,
        )
        if html:
            try:
                send_divergence_alert(html, f"[一致预期] 两源背离 {len(divergent_all)} 项 — {fetched_at[:10]}")
            except Exception as e:
                print(f"WARN: 告警邮件发送失败（{type(e).__name__}: {e}）", file=sys.stderr)

    if errors:
        print(f"\n=== FAILED ({len(errors)}/{len(stocks)}) ===", file=sys.stderr)
        for msg in errors:
            print(f"  {msg}", file=sys.stderr)
        return 1

    print(f"\n=== done ({len(stocks) - len(skipped_hk)} stocks, {len(flagged_all)} flagged) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
