#!/usr/bin/env python3
"""
update_research_snapshots.py — 每日快照更新脚本

每个在 config/research_stocks.json 中注册的研究股票：
1. 通过腾讯 qt 实时行情（主）/ 东方财富 push2（备）获取最新收盘价、总市值
   （2026-06-02 起 push2 海外边缘节点对本 EC2 返回 502，国内访问正常，故腾讯为主源）
2. 通过腾讯财经 K 线获取近 252 交易日收盘价并计算 1 年涨幅
3. 用市值除以各期共识净利润，计算动态 PE
4. 写出 docs-site/data/{key}-snapshot.json
5. 发布 JSON 文件到 /var/www/overview/data/

脚本任意股票失败都 exit(1)，由 cron-wrapper.sh 触发告警邮件。
"""

import json
import os
import pathlib
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ── HTTP retry helper ──────────────────────────────────────────────────────────


def _get_with_retry(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 15,
    max_attempts: int = 3,
    backoff: float = 1.0,
    label: str = "",
) -> requests.Response:
    """HTTP GET with retry on 5xx and network errors.

    4xx fails immediately (client-side bug shouldn't be retried). 5xx +
    ConnectionError + Timeout trigger exponential backoff. Last-attempt
    failure propagates the original exception. ``label`` shows up in retry
    warnings, useful when multiple stocks/endpoints share this code path.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt >= max_attempts:
                raise
            wait = backoff * (2 ** (attempt - 1))
            print(
                f"WARN: {label} {type(e).__name__}, retrying "
                f"({attempt + 1}/{max_attempts}) in {wait:.1f}s...",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait)
            continue
        if r.status_code >= 500 and attempt < max_attempts:
            wait = backoff * (2 ** (attempt - 1))
            print(
                f"WARN: {label} HTTP {r.status_code}, retrying "
                f"({attempt + 1}/{max_attempts}) in {wait:.1f}s...",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("_get_with_retry: unreachable")


# ── paths ──────────────────────────────────────────────────────────────────────
REPO_DIR = pathlib.Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_DIR / "config" / "research_stocks.json"

DOCS_SITE_DIR = pathlib.Path(os.path.expanduser("~/docs-site"))
DATA_DIR = DOCS_SITE_DIR / "data"
DEPLOY_DATA_DIR = pathlib.Path("/var/www/overview/data")

BJT = timezone(timedelta(hours=8))

# ── East Money push2delay ──────────────────────────────────────────────────────
# push2delay 而非 push2：push2 海外边缘节点 2026-06-02 起对本 EC2 返回 502；
# push2delay 字段与 push2 逐字节一致（已对照国内 IP 验证），且未封海外。
_EM_URL = "https://push2delay.eastmoney.com/api/qt/stock/get"
_EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_EM_FIELDS = "f57,f58,f43,f116,f162,f163,f170,f47"
# f57=symbol, f58=name, f43=price(×100), f116=市值(元), f162=PE-TTM-s(×100), f163=PE-TTM-d(×100)
# f170=涨跌幅%(×100), f47=成交量(手)


def em_secid(symbol: str, exchange: str) -> str:
    if exchange == "SH":
        return f"1.{symbol}"
    if exchange == "SZ":
        return f"0.{symbol}"
    if exchange == "HK":
        return f"116.{symbol}"
    raise ValueError(f"Unknown exchange: {exchange}")


def fetch_em_data(symbol: str, exchange: str) -> dict:
    """获取东方财富 push2delay 行情（备源）。返回 price_yuan, market_cap_yuan。"""
    secid = em_secid(symbol, exchange)
    r = _get_with_retry(
        _EM_URL,
        params={"secid": secid, "fields": _EM_FIELDS, "ut": _EM_UT},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
        label=f"[{symbol}] push2",
    )
    data = r.json().get("data")
    if not data:
        raise ValueError(f"push2 returned empty data for {symbol}")

    raw_price = data.get("f43") or 0
    market_cap = data.get("f116") or 0
    if not raw_price or not market_cap:
        raise ValueError(f"push2 missing price or market_cap for {symbol}: f43={raw_price}, f116={market_cap}")

    price_yuan = round(raw_price / 100, 2)
    _vc = data.get("f170")
    raw_change = int(_vc) if isinstance(_vc, (int, float)) else 0
    _vv = data.get("f47")
    raw_vol = int(_vv) if isinstance(_vv, (int, float)) else 0
    change_pct = round(raw_change / 100, 2)
    vol_wan_shou = round(raw_vol / 10000, 1)
    return {
        "price_yuan": price_yuan,
        "market_cap_yuan": market_cap,
        "change_pct": change_pct,
        "vol_wan_shou": vol_wan_shou,
    }


# ── Tencent qt 实时行情 ─────────────────────────────────────────────────────────
_QT_URL = "https://qt.gtimg.cn/q={code}"
# ~分隔字段: [3]=现价, [32]=涨跌幅%, [36]=成交量, [45]=总市值(亿)
# 成交量单位: 科创板(688)=股, 其他板块=手（2026-06-03 用换手率字段交叉验证）


def qt_code(symbol: str, exchange: str) -> str:
    if exchange == "SH":
        return f"sh{symbol}"
    if exchange == "SZ":
        return f"sz{symbol}"
    if exchange == "HK":
        return f"hk{symbol}"
    raise ValueError(f"Unknown exchange: {exchange}")


def fetch_qt_data(symbol: str, exchange: str) -> dict[str, float]:
    """获取腾讯 qt 实时行情。返回结构与 fetch_em_data 一致。"""
    code = qt_code(symbol, exchange)
    r = _get_with_retry(
        _QT_URL.format(code=code),
        headers={"Referer": "https://gu.qq.com/"},
        timeout=15,
        label=f"[{symbol}] qt",
    )
    r.encoding = "gbk"
    txt = r.text.strip()
    if "=" not in txt:
        raise ValueError(f"qt malformed response for {symbol}: {txt[:60]}")
    fields = txt.split("=", 1)[1].strip().strip('";').split("~")
    if len(fields) < 46:
        raise ValueError(f"qt too few fields for {symbol}: len={len(fields)}")

    price_yuan = round(float(fields[3]), 2) if fields[3] else 0.0
    market_cap_yi = float(fields[45]) if fields[45] else 0.0
    if price_yuan <= 0 or market_cap_yi <= 0:
        raise ValueError(
            f"qt missing price or market_cap for {symbol}: price={fields[3]}, cap={fields[45]}"
        )

    change_pct = round(float(fields[32]), 2) if fields[32] else 0.0
    raw_vol = float(fields[36]) if fields[36] else 0.0
    # 科创板(688)成交量单位是股，其他板块是手
    vol_shou = raw_vol / 100 if symbol.startswith("688") else raw_vol
    return {
        "price_yuan": price_yuan,
        "market_cap_yuan": market_cap_yi * 1e8,
        "change_pct": change_pct,
        "vol_wan_shou": round(vol_shou / 10000, 1),
    }


# ── 实时行情统一入口：腾讯主 + push2 备 ──────────────────────────────────────────


def fetch_quote_data(symbol: str, exchange: str) -> dict[str, float]:
    """实时行情：腾讯 qt 主源，东方财富 push2 备用。

    2026-06-02 起 push2 海外边缘节点对本 EC2 持续返回 502（国内访问正常），
    故腾讯为主源；push2 保留为备用，若未来恢复海外访问可自动受益。
    """
    try:
        return fetch_qt_data(symbol, exchange)
    except Exception as e:  # noqa: BLE001 — 任何主源异常都应触发备源
        print(
            f"WARN: [{symbol}] 腾讯 qt 失败 ({type(e).__name__}: {e})，回退 push2...",
            file=sys.stderr,
            flush=True,
        )
        return fetch_em_data(symbol, exchange)


# ── Tencent K-line ─────────────────────────────────────────────────────────────
_TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def fetch_ohlcv_data(symbol: str, exchange: str) -> dict:
    """获取近一年前复权 OHLCV，计算年涨幅 + MA20 + 波动率 + 量比。
    腾讯 K 线行格式: [date, open, close, high, low, volume, amount, ...]
    """
    import math

    mkt = {"SH": "sh", "SZ": "sz", "HK": "hk"}.get(exchange, "sz")
    code = f"{mkt}{symbol}"
    end_dt = datetime.now(BJT)
    start_dt = end_dt - timedelta(days=366)
    r = _get_with_retry(
        _TENCENT_URL,
        params={"param": f"{code},day,{start_dt:%Y-%m-%d},{end_dt:%Y-%m-%d},300,qfq"},
        headers={"Referer": "https://gu.qq.com/"},
        timeout=15,
        label=f"[{symbol}] tencent-kline",
    )
    d = r.json()
    stock_data = d.get("data", {}).get(code, {})
    rows = stock_data.get("qfqday") or stock_data.get("day", [])
    return compute_technicals(rows, symbol=symbol)


_TRADING_DAYS_PER_YEAR = 252
# 「满一年」按日历跨度判定,不按 K 线根数。生产端只取近 366 天,A 股一年实测约 241 根
# (中际旭创 2026-08-03 实测 241),用 252 根做门槛会把所有存量股票误判成历史不足。
# 留 15 天余量吸收长假与停牌。
_FULL_YEAR_SPAN_DAYS = 350


def _spans_full_year(rows: list) -> bool:
    """K 线首末日期跨度是否够一年。日期解析不了时保守返回 False(宁可标不足)。"""
    if len(rows) < 2:
        return False
    try:
        first = datetime.strptime(str(rows[0][0])[:10], "%Y-%m-%d").date()
        last = datetime.strptime(str(rows[-1][0])[:10], "%Y-%m-%d").date()
    except (ValueError, IndexError, TypeError):
        return False
    return (last - first).days >= _FULL_YEAR_SPAN_DAYS


def compute_technicals(rows: list, symbol: str = "", today=None) -> dict:
    """腾讯 K 线行 → 技术指标。行格式 [date, open, close, high, low, volume, amount, ...]。

    **历史长度守卫**:每个指标只在数据真的够的时候才给值，不够就是 None。
    次新股(长鑫科技 688825 上市 6 个交易日、智谱 02513 上市 138 个交易日)是这里的
    主要约束——旧实现在不足 252 根时会静默把短窗口涨幅写进 `year_return_pct`，
    让页面把 138 日的 +629.7% 标成「1 年涨幅」。数据不够就说不够，不拿短窗口冒充。

    `history_days` / `period_return_pct` / `week52_is_full` 是为此新增的字段，
    下游可据此显示「上市以来 +X%（N 个交易日）」而不是假的年度指标。
    """
    import math

    if not rows:
        raise ValueError(f"No K-line rows for {symbol or 'stock'}")

    closes  = [float(row[2]) for row in rows if len(row) >= 3]
    highs   = [float(row[3]) for row in rows if len(row) >= 5]
    lows    = [float(row[4]) for row in rows if len(row) >= 5]
    volumes = [float(row[5]) for row in rows if len(row) >= 6]

    if not closes:
        raise ValueError(f"No usable close prices for {symbol or 'stock'}")

    history_days = len(closes)
    has_full_year = _spans_full_year(rows)

    # 1 年涨幅：抓取窗口本身就是近 366 天，满一年时整段即为年涨幅
    year_return_pct: float | None = None
    if has_full_year and closes[0]:
        year_return_pct = round((closes[-1] / closes[0] - 1) * 100, 1)

    # 全窗口涨幅：次新股用它代替年涨幅（配合 history_days 才有意义）
    period_return_pct = round((closes[-1] / closes[0] - 1) * 100, 1) if closes[0] else None

    # MA20（当日 + 5 日前，判断斜率）；斜率算不出时为 None，不默认 "down"
    ma20 = round(sum(closes[-20:]) / 20, 2) if history_days >= 20 else None
    ma20_5d = round(sum(closes[-25:-5]) / 20, 2) if history_days >= 25 else None
    ma20_slope = None
    if ma20 is not None and ma20_5d is not None:
        ma20_slope = "up" if ma20 > ma20_5d else "down"

    # 60 日年化波动率（对数收益率标准差 × √252）
    vol_60d_ann_pct: float | None = None
    if history_days >= 61:
        lr = [math.log(closes[-60 + i + 1] / closes[-60 + i]) for i in range(59)]
        daily_std = math.sqrt(sum(x * x for x in lr) / len(lr))
        vol_60d_ann_pct = round(daily_std * math.sqrt(_TRADING_DAYS_PER_YEAR) * 100, 1)

    # 5 日 / 60 日量比
    vol_ratio_5_60: float | None = None
    if len(volumes) >= 60:
        avg5 = sum(volumes[-5:]) / 5
        avg60 = sum(volumes[-60:]) / 60
        vol_ratio_5_60 = round(avg5 / avg60, 2) if avg60 > 0 else None

    # 52 周高低：抓取窗口即近一年；不满一年时这是「上市以来」极值，用 week52_is_full 标出
    week52_is_full = has_full_year
    week52_high = round(max(highs), 2) if highs else None
    week52_low  = round(min(lows), 2) if lows else None

    # 高点日期与时效：**脱离时间的回撤没有意义** —— 两周跌 33% 与 11 个月阴跌 33%
    # 是两回事。买点逻辑靠这两个字段区分「近期急跌」与「长期阴跌」。
    high_date = None
    if highs:
        peak = max(range(len(highs)), key=lambda i: highs[i])
        try:
            high_date = str(rows[peak][0])[:10]
            datetime.strptime(high_date, "%Y-%m-%d")
        except (ValueError, IndexError, TypeError):
            high_date = None
    high_age = None
    if high_date:
        ref = today or datetime.now(BJT).date()
        high_age = (ref - datetime.strptime(high_date, "%Y-%m-%d").date()).days

    return {
        "year_return_pct": year_return_pct,
        "period_return_pct": period_return_pct,
        "history_days": history_days,
        "ma20": ma20,
        "ma20_slope": ma20_slope,
        "vol_60d_ann_pct": vol_60d_ann_pct,
        "vol_ratio_5_60": vol_ratio_5_60,
        "week52_high": week52_high,
        "week52_low": week52_low,
        "week52_is_full": week52_is_full,
        "week52_high_date": high_date,
        "week52_high_age_days": high_age,
    }


# ── 估值分母来源：自动抓取优先，注册表兜底 ────────────────────────────────────
#
# 2026-08-04 审计发现：`update_research_consensus.py` 的产物落在
# `{key}-consensus.json`，但本脚本算 PE/PS 一直用 `research_stocks.json` 的冻结值，
# 而研报页读的是本脚本的快照 → **页面估值倍数一直是化石**
# （寒武纪 2027E PE 页面 90.3 vs 应为 59.3，高估 52%）。
# 同时观察池卡片已接 consensus.json，导致同一网站两处 PE 不一致。
#
# 注册表当「人工权威口径」的原意是「自动抓取需人工把关」，但把关已由双源交叉复核 +
# 中位数 + 离群标记 + 背离告警自动化，再留人工闸门只会制造化石。故改为自动优先。


def load_consensus_estimates(snapshot_key: str, data_dir: pathlib.Path | None = None) -> dict:
    """读 `{key}-consensus.json` 的 estimates。文件缺失/损坏一律返回 {} 交由兜底。"""
    base = data_dir if data_dir is not None else DATA_DIR
    f = pathlib.Path(base) / f"{snapshot_key}-consensus.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("estimates") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_consensus(stock: dict, data_dir: pathlib.Path | None = None) -> tuple[dict, str]:
    """返回 `(estimates, source)`，source ∈ {"auto", "registry"}。

    自动源为空（抓取失败留下的空壳）时回落注册表——**不能让空文件把估值分母清空**。
    """
    auto = load_consensus_estimates(stock.get("snapshot_key", ""), data_dir)
    # 自动源同时含实际年份（'2025A' 等，来自同花顺详细指标表的「实际值」列）与预测年份。
    # **用今天的市值除以历史年份的利润没有意义**，只保留预测年（后缀 E）。
    forecast = {y: v for y, v in auto.items() if str(y).endswith("E")}
    if forecast:
        return forecast, "auto"
    return stock.get("consensus") or {}, "registry"


# ── 币种换算（港股）────────────────────────────────────────────────────────────
#
# 智谱 02513.HK 的市值报价是 HKD，而机构一致预期的营收/净利是 CNY
# （yfinance: currency=HKD, financialCurrency=CNY）。直接相除 → PS 系统性偏高约 16%
# （HKDCNY 实测 0.8604）。A 股报价与财务同为 CNY，不触发换算。

_QUOTE_CCY = {"SH": "CNY", "SZ": "CNY", "HK": "HKD"}
_FX_TICKER = {("HKD", "CNY"): "HKDCNY=X"}


def quote_currency(exchange: str) -> str:
    """交易所 → 报价币种。"""
    return _QUOTE_CCY.get(exchange, "CNY")


def convert_market_cap(cap: float, from_ccy: str, to_ccy: str, fx_rate: float | None) -> float:
    """把市值换算到一致预期所用的币种。

    币种相同则原样返回（不做乘 1.0，避免浮点漂移）。
    **拿不到汇率时抛错而不是跳过换算** —— 静默跳过会产出偏高 16% 的 PS 且无人察觉，
    这正是本函数存在的原因。
    """
    if from_ccy == to_ccy:
        return cap
    if not fx_rate or fx_rate <= 0:
        raise ValueError(f"缺少有效汇率 {from_ccy}->{to_ccy}（得到 {fx_rate!r}），拒绝跳过换算")
    return cap * fx_rate


_FX_TIMEOUT_S = 30


def call_with_timeout(fn, timeout_s: int, label: str = ""):
    """给没有 timeout 参数的调用施加硬性上限（daemon 线程 + join）。

    yfinance 爬 Yahoo，与 akshare 一样可能无限期挂起；而本脚本的 cron 只有 300s
    且**每交易日**运行——一次挂起会让全池 11 只都拿不到快照。
    worker 抛出的真实异常原样传播，不伪装成超时。
    """
    import threading

    box: dict = {}

    def _worker() -> None:
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001 —— 需原样带回主线程
            box["error"] = e

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        raise TimeoutError(f"{label or 'call'} 超过 {timeout_s}s 未返回（上游疑似挂起）")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _fetch_fx_rate_raw(ticker: str) -> float:
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="5d")
    if hist is None or hist.empty:
        raise ValueError(f"{ticker} 无汇率数据")
    return float(hist["Close"].iloc[-1])


def fetch_fx_rate(from_ccy: str, to_ccy: str) -> float:
    """取即期汇率（yfinance）。仅在港股路径被调用，带硬超时。"""
    ticker = _FX_TICKER.get((from_ccy, to_ccy))
    if not ticker:
        raise ValueError(f"未配置汇率代码 {from_ccy}->{to_ccy}")
    return call_with_timeout(lambda: _fetch_fx_rate_raw(ticker),
                             _FX_TIMEOUT_S, label=f"fx[{ticker}]")


def format_return_summary(snapshot: dict) -> str:
    """控制台摘要里的涨幅串。

    次新股没有 1 年涨幅,退回「上市以来 +X% (N日)」——既不崩,也不冒充年度指标。
    """
    year = snapshot.get("year_return_pct")
    if year is not None:
        return f"1年{year:+.1f}%"
    period = snapshot.get("period_return_pct")
    days = snapshot.get("history_days")
    if period is not None and days is not None:
        return f"上市以来{period:+.1f}% ({days}日)"
    return "涨幅—"


# ── snapshot writer ─────────────────────────────────────────────────────────────

def build_snapshot(stock: dict) -> dict:
    symbol = stock["symbol"]
    exchange = stock["exchange"]

    print(f"  [{symbol}] 拉取实时行情 (腾讯 qt 主 / push2 备)...", flush=True)
    quote = fetch_quote_data(symbol, exchange)
    price_yuan = quote["price_yuan"]
    market_cap_yuan = quote["market_cap_yuan"]
    change_pct = quote["change_pct"]
    vol_wan_shou = quote["vol_wan_shou"]

    print(f"  [{symbol}] 拉取腾讯 K 线...", flush=True)
    ohlcv = fetch_ohlcv_data(symbol, exchange)

    market_cap_yi = round(market_cap_yuan / 1e8)  # 转换为亿
    as_of = datetime.now(BJT).strftime("%Y-%m-%d")

    # 估值分母的币种：注册表显式声明；未声明则视同报价币种（A 股即 CNY）
    q_ccy = quote_currency(exchange)
    c_ccy = stock.get("consensus_currency", q_ccy)
    fx_rate = None
    if c_ccy != q_ccy:
        fx_rate = fetch_fx_rate(q_ccy, c_ccy)
        print(f"  [{symbol}] 汇率 {q_ccy}->{c_ccy} = {fx_rate:.4f}", flush=True)
    # 仅用于估值分母对齐；展示用的 market_cap_yi 仍保持报价币种
    valuation_cap = convert_market_cap(market_cap_yuan, q_ccy, c_ccy, fx_rate)

    consensus, consensus_source = resolve_consensus(stock)

    valuation_mode = stock.get("valuation_mode", "pe")
    pe_estimates: dict[str, float] = {}
    ps_estimates: dict[str, float] = {}
    for label, entry in consensus.items():
        if valuation_mode in ("ps", "both") and "revenue_yuan" in entry:
            ps_estimates[label] = round(valuation_cap / entry["revenue_yuan"], 1)
        if valuation_mode in ("pe", "both") and "profit_yuan" in entry:
            pe_estimates[label] = round(valuation_cap / entry["profit_yuan"], 1)

    snapshot = {
        "symbol": symbol,
        "name": stock["name"],
        "as_of": as_of,
        "price_yuan": price_yuan,
        "change_pct": change_pct,
        "vol_wan_shou": vol_wan_shou,
        "market_cap_yi": market_cap_yi,
        # 次新股 year_return_pct 为 None,改看 period_return_pct + history_days
        "year_return_pct": ohlcv["year_return_pct"],
        "period_return_pct": ohlcv.get("period_return_pct"),
        "history_days": ohlcv.get("history_days"),
        "pe_estimates": pe_estimates,
        "ps_estimates": ps_estimates,
        # 汇率与币种落盘，否则事后无法复算 PS/PE 是怎么来的
        "consensus_source": consensus_source,
        "quote_currency": q_ccy,
        "consensus_currency": c_ccy,
        "fx_rate": round(fx_rate, 6) if fx_rate else None,
        "technical": {
            "ma20": ohlcv["ma20"],
            "ma20_slope": ohlcv["ma20_slope"],
            "vol_60d_ann_pct": ohlcv["vol_60d_ann_pct"],
            "vol_ratio_5_60": ohlcv["vol_ratio_5_60"],
            "week52_high": ohlcv["week52_high"],
            "week52_low": ohlcv["week52_low"],
            "week52_is_full": ohlcv.get("week52_is_full"),
            "week52_high_date": ohlcv.get("week52_high_date"),
            "week52_high_age_days": ohlcv.get("week52_high_age_days"),
        },
        "updated_at": datetime.now(BJT).isoformat(),
    }
    return snapshot


def write_and_deploy(key: str, snapshot: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEPLOY_DATA_DIR.mkdir(parents=True, exist_ok=True)

    json_str = json.dumps(snapshot, ensure_ascii=False, indent=2)
    src = DATA_DIR / f"{key}-snapshot.json"
    src.write_text(json_str, encoding="utf-8")

    dst = DEPLOY_DATA_DIR / f"{key}-snapshot.json"
    shutil.copy2(src, dst)
    print(f"  [{key}] snapshot 写出: {src} → {dst}", flush=True)


# ── main ────────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"=== update_research_snapshots ({datetime.now(BJT):%Y-%m-%d %H:%M} BJT) ===")

    if not CONFIG_FILE.exists():
        print(f"ERROR: config not found: {CONFIG_FILE}", file=sys.stderr)
        return 1

    stocks = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not stocks:
        print("WARNING: research_stocks.json is empty — nothing to do")
        return 0

    errors: list[str] = []

    for stock in stocks:
        symbol = stock["symbol"]
        try:
            snapshot = build_snapshot(stock)
            write_and_deploy(stock["snapshot_key"], snapshot)
            market_cap_yi = snapshot["market_cap_yi"]
            price = snapshot["price_yuan"]
            ret_str = format_return_summary(snapshot)
            if snapshot["ps_estimates"]:
                val_str = "  ".join(f"{k}={v}x PS" for k, v in snapshot["ps_estimates"].items())
            else:
                val_str = "  ".join(f"{k}={v}x" for k, v in snapshot["pe_estimates"].items())
            print(f"  [{symbol}] ✓  ¥{price}  市值{market_cap_yi}亿  {ret_str}  {val_str}")
        except Exception as e:
            msg = f"[{symbol}] FAILED: {e}"
            print(f"ERROR: {msg}", file=sys.stderr)
            errors.append(msg)
        time.sleep(0.5)

    if errors:
        print(f"\n=== FAILED ({len(errors)}/{len(stocks)}) ===", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print(f"\n=== done ({len(stocks)} stocks updated) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
