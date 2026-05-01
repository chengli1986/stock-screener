#!/usr/bin/env python3
"""
成长门槛探针 — 沪深300 + 中证500 双门槛灵敏度分析

使用东方财富 datacenter API（24小时可用，支持多报告期），对全量约800只A股
拉取近N期财报数据，分析不同增速门槛下的候选池密度，并检验业绩连续性。

Usage:
  python3 scripts/growth_gate_probe.py              # 全量 ~800只，取最新3期
  python3 scripts/growth_gate_probe.py --sample 80  # 快速样本 (~2min)
  python3 scripts/growth_gate_probe.py --periods 1  # 只看最新1期（最快）
  python3 scripts/growth_gate_probe.py --use-cache  # 复用上次结果，跳过拉取

数据来源: datacenter-web.eastmoney.com（非 push2，24h 可用，非交易日可跑）
"""
import argparse
import json
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import akshare as ak

BJT = timezone(timedelta(hours=8))
REPO_DIR = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_DIR / "artifacts" / "growth-gate-probe"
CACHE_JSONL = ARTIFACTS_DIR / "fundamentals.jsonl"

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}
DC_COLUMNS = ",".join([
    "SECURITY_CODE", "SECURITY_NAME_ABBR", "REPORT_DATE", "REPORT_DATE_NAME",
    "TOTALOPERATEREVETZ",   # 营收同比 %
    "PARENTNETPROFITTZ",    # 净利润同比 %
    "PARENTNETPROFIT",      # 净利润绝对值（判断上期是否亏损）
])
BATCH_SIZE = 50  # 每次API请求的股票数


def fetch_universe() -> list[dict]:
    print("拉取沪深300 + 中证500成分股 (csindex.com.cn)...")
    csi300 = ak.index_stock_cons_weight_csindex(symbol="000300")
    csi500 = ak.index_stock_cons_weight_csindex(symbol="000905")

    seen: set[str] = set()
    universe: list[dict] = []
    for df, src in [(csi300, "csi300"), (csi500, "csi500")]:
        for _, row in df.iterrows():
            code = str(row["成分券代码"])
            if code not in seen:
                seen.add(code)
                universe.append({
                    "symbol": code,
                    "name": str(row["成分券名称"]),
                    "source": src,
                })
    print(f"Universe: {len(universe)} 只唯一A股")
    return universe


def fetch_batch(symbols: list[str], periods: int, batch_idx: int = 0) -> dict[str, list[dict]]:
    """
    批量拉取一批股票的近N期财报数据。
    返回 {symbol: [期1数据, 期2数据, ...]} 字典。
    """
    codes_str = ",".join(f'"{s}"' for s in symbols)
    filter_str = f"(SECURITY_CODE in ({codes_str}))"
    page_size = len(symbols) * periods

    result: dict[str, list[dict]] = {s: [] for s in symbols}
    try:
        r = requests.get(
            DC_URL,
            params={
                "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                "columns": DC_COLUMNS,
                "filter": filter_str,
                "pageSize": page_size,
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
            },
            headers=DC_HEADERS,
            timeout=20,
        )
        if r.status_code != 200:
            return result
        d = r.json()
        if not d.get("success") or not d.get("result"):
            return result

        for row in d["result"]["data"]:
            code = row.get("SECURITY_CODE", "")
            if code in result and len(result[code]) < periods:
                result[code].append({
                    "report_date": str(row.get("REPORT_DATE", ""))[:10],
                    "period_name": row.get("REPORT_DATE_NAME", ""),
                    "revenue_growth": row.get("TOTALOPERATEREVETZ"),
                    "net_profit_growth": row.get("PARENTNETPROFITTZ"),
                    "net_profit": row.get("PARENTNETPROFIT"),
                })
        return result
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"[批次 {batch_idx}] 请求失败: {e}", flush=True)
        return result


def load_cache() -> dict[str, list[dict]]:
    cache: dict[str, list[dict]] = {}
    if CACHE_JSONL.exists():
        for line in CACHE_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rec = json.loads(line)
                cache[rec["symbol"]] = rec["periods"]
    return cache


def save_cache(symbol: str, periods_data: list[dict]) -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with CACHE_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"symbol": symbol, "periods": periods_data},
                            ensure_ascii=False) + "\n")


def fetch_all(universe: list[dict], periods: int) -> dict[str, list[dict]]:
    """拉取全部股票的近N期数据，批量请求。"""
    symbols = [u["symbol"] for u in universe]
    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]

    print(f"批量拉取 {len(symbols)} 只股票近{periods}期数据，"
          f"共{len(batches)}批次（每批{BATCH_SIZE}只）...")
    eta = len(batches) * 2.5
    print(f"预计耗时: {eta/60:.0f}-{eta*1.3/60:.0f} 分钟\n")

    results: dict[str, list[dict]] = {}
    for i, batch in enumerate(batches, 1):
        batch_result = fetch_batch(batch, periods, batch_idx=i)
        ok_count = sum(1 for v in batch_result.values() if v)
        print(f"[批次 {i:2}/{len(batches)}] {len(batch)} 只 → {ok_count} 只拿到数据", flush=True)
        results.update(batch_result)
        time.sleep(0.5)

    # 写缓存
    if CACHE_JSONL.exists():
        CACHE_JSONL.unlink()
    for symbol, pdata in results.items():
        save_cache(symbol, pdata)

    return results


# ── 分析逻辑 ────────────────────────────────────────────────────────────────

def passes_gate(periods_data: list[dict], thresh: float) -> bool:
    """
    最新期营收+净利润同比均 ≥ thresh。

    旧版有 ≤200% 上限兜底（原意：过滤由亏转盈的基数效应）。
    改为精确判断：若最新期净利润 > 0 则不设上限；
    若最新期净利润 ≤ 0（本期亏损，增速无意义）则直接排除。
    这样中际旭创（+262% 但本期盈利 57 亿）不再被误杀。
    """
    if not periods_data:
        return False
    latest = periods_data[0]
    rg = latest.get("revenue_growth")
    ng = latest.get("net_profit_growth")
    np_val = latest.get("net_profit")
    if rg is None or ng is None:
        return False
    # 本期亏损则排除（增速为负或分母为负时无意义）
    if np_val is not None and np_val <= 0:
        return False
    return rg >= thresh and ng >= thresh


def passes_continuity(periods_data: list[dict], min_thresh: float, n: int) -> bool:
    """近n期营收同比和净利润同比均 ≥ min_thresh。"""
    if len(periods_data) < n:
        return False
    for p in periods_data[:n]:
        rg = p.get("revenue_growth")
        ng = p.get("net_profit_growth")
        if rg is None or ng is None or rg < min_thresh or ng < min_thresh:
            return False
    return True


def analyze(universe: list[dict], results: dict[str, list[dict]], periods: int) -> None:
    # 构建有效记录（最新期双字段均不为None）
    name_map = {u["symbol"]: u["name"] for u in universe}
    records: list[dict] = []
    for symbol, pdata in results.items():
        if not pdata:
            continue
        latest = pdata[0]
        rg = latest.get("revenue_growth")
        ng = latest.get("net_profit_growth")
        if rg is None or ng is None:
            continue
        records.append({
            "symbol": symbol,
            "name": name_map.get(symbol, ""),
            "periods": pdata,
            "revenue_growth": rg,
            "net_profit_growth": ng,
            "period_name": latest.get("period_name", ""),
        })

    total = len(universe)
    data_ok = len(records)

    print(f"\n{'='*65}")
    print(f"  成长门槛探针  {datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')}")
    print(f"{'='*65}")
    print(f"  Universe总数:     {total}")
    print(f"  双字段有效:       {data_ok}  (缺失/失败: {total - data_ok})")
    if records:
        print(f"  数据报告期:       {records[0]['period_name']}（最新期）")

    # ── 门槛灵敏度扫描（仅看最新1期）──────────────────────────────────────
    print(f"\n── 门槛灵敏度（最新1期：营收同比 AND 净利润同比 均 ≥ X%，+≤200%上限）──")
    print(f"  {'门槛':>5}  {'通过':>5}  {'占比':>6}  备注")
    print("  " + "-" * 45)
    for thresh in [10, 15, 20, 25, 30, 35, 40, 50]:
        passed = [r for r in records if passes_gate(r["periods"], thresh)]
        pct = len(passed) / data_ok * 100 if data_ok else 0
        marker = ""
        if 60 <= len(passed) <= 120:
            marker = "  ← 目标区间"
        elif len(passed) < 30:
            marker = "  ← 候选池过稀"
        elif len(passed) > 200:
            marker = "  ← 候选池过密"
        print(f"  ≥{thresh:>3}%  {len(passed):>5}  {pct:>5.1f}%{marker}")

    # ── 连续性分析（如果拉了多期）──────────────────────────────────────────
    if periods >= 2:
        print(f"\n── 连续性门槛（最新1期≥30% + 近N期均≥15%）──")
        base_30 = [r for r in records if passes_gate(r["periods"], 30)]
        print(f"  最新期 ≥30%/30%:         {len(base_30)} 只")
        for n in range(2, min(periods + 1, 5)):
            cont = [r for r in base_30 if passes_continuity(r["periods"], 15, n)]
            pct = len(cont) / len(base_30) * 100 if base_30 else 0
            print(f"  + 近{n}期均≥15%:         {len(cont):>4} 只  (淘汰率 {100-pct:.0f}%)")

    # ── 30%通过名单 ─────────────────────────────────────────────────────────
    passed_30 = sorted(
        [r for r in records if passes_gate(r["periods"], 30)],
        key=lambda x: -x["net_profit_growth"],
    )
    print(f"\n── 30%/30%双门槛通过名单（{len(passed_30)} 只，按净利同比降序）──")
    print(f"  {'代码':8} {'名称':10} {'营收同比':>9} {'净利同比':>9}  报告期")
    print("  " + "-" * 58)
    for r in passed_30:
        print(f"  {r['symbol']:8} {r['name'][:8]:10} "
              f"{r['revenue_growth']:>8.1f}% {r['net_profit_growth']:>8.1f}%  "
              f"{r['period_name']}")

    # ── 增速分布直方图 ──────────────────────────────────────────────────────
    buckets = [(-999, -50), (-50, 0), (0, 10), (10, 20), (20, 30),
               (30, 50), (50, 100), (100, 200), (200, 9999)]
    scale = max(data_ok // 60, 1)

    print(f"\n── 营收同比分布（{data_ok} 只，每█代表约{scale}只）──")
    for lo, hi in buckets:
        cnt = sum(1 for r in records if lo <= r["revenue_growth"] < hi)
        hi_s = " +∞ " if hi == 9999 else f"{hi:4}%"
        bar = "█" * (cnt // scale)
        flag = "  ← 30%门槛" if lo == 30 else ""
        print(f"  [{lo:>4}%, {hi_s})  {cnt:4}  {bar}{flag}")

    print(f"\n── 净利润同比分布（{data_ok} 只，每█代表约{scale}只）──")
    for lo, hi in buckets:
        cnt = sum(1 for r in records if lo <= r["net_profit_growth"] < hi)
        hi_s = " +∞ " if hi == 9999 else f"{hi:4}%"
        bar = "█" * (cnt // scale)
        flag = "  ← 30%门槛" if lo == 30 else ""
        print(f"  [{lo:>4}%, {hi_s})  {cnt:4}  {bar}{flag}")

    print(f"\n结果已缓存: {CACHE_JSONL}")
    print("下次可用 --use-cache 跳过拉取")


# ── 主入口 ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="成长门槛探针")
    parser.add_argument("--sample", type=int, default=0,
                        help="随机抽取N只股票（0=全量约800只）")
    parser.add_argument("--periods", type=int, default=3,
                        help="拉取近N个报告期，用于连续性检验（默认3）")
    parser.add_argument("--use-cache", action="store_true",
                        help="复用上次JSONL缓存，不重新拉取")
    args = parser.parse_args()

    universe = fetch_universe()
    if args.sample > 0:
        universe = random.sample(universe, min(args.sample, len(universe)))
        print(f"随机抽样: {len(universe)} 只")

    if args.use_cache and CACHE_JSONL.exists():
        print(f"复用缓存: {CACHE_JSONL}")
        results = load_cache()
    else:
        results = fetch_all(universe, args.periods)

    analyze(universe, results, args.periods)


if __name__ == "__main__":
    main()
