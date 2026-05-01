#!/usr/bin/env python3
"""
成长门槛探针 — 沪深300 + 中证500 双门槛灵敏度分析

对全量约800只A股拉东方财富push2基本面，分析不同增速门槛下的候选池密度，
帮助在写完整框架之前验证阈值是否合理。

Usage:
  python3 scripts/growth_gate_probe.py              # 全量 ~800只 (~20min)
  python3 scripts/growth_gate_probe.py --sample 80  # 快速样本 (~3min)
  python3 scripts/growth_gate_probe.py --use-cache  # 复用上次JSONL结果，跳过拉取
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

# push2 字段：营收同比f184 净利润同比f185 行业f127 市值f116 PE_TTM(静)f162 PE_TTM(动)f163
EM_FIELDS = "f57,f58,f116,f127,f162,f163,f184,f185"


def em_secid(symbol: str) -> str:
    return f"1.{symbol}" if symbol.startswith(("6", "9")) else f"0.{symbol}"


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


def fetch_one(symbol: str) -> dict:
    secid = em_secid(symbol)
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": EM_FIELDS,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    base: dict = {"symbol": symbol, "ok": False, "error": ""}
    try:
        r = requests.get(url, params=params, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            base["error"] = f"HTTP {r.status_code}"
            return base
        data = r.json().get("data")
        if not data:
            base["error"] = "empty data"
            return base

        # 用 or None 把 0.0（API缺失）转为None，与 phase0_spike 一致
        revenue_g = data.get("f184") or None
        net_profit_g = data.get("f185") or None
        pe_raw = data.get("f162") or data.get("f163") or None

        return {
            "symbol": symbol,
            "ok": True,
            "revenue_growth": revenue_g,
            "net_profit_growth": net_profit_g,
            "industry": data.get("f127") or "",
            "market_cap": data.get("f116") or None,
            "pe_ttm": round(pe_raw / 100, 2) if pe_raw else None,
        }
    except Exception as exc:
        base["error"] = str(exc)
        return base


def load_cache() -> dict[str, dict]:
    cache: dict[str, dict] = {}
    if CACHE_JSONL.exists():
        for line in CACHE_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rec = json.loads(line)
                cache[rec["symbol"]] = rec
    return cache


def save_record(rec: dict) -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with CACHE_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 分析输出 ────────────────────────────────────────────────────────────────

def analyze(universe: list[dict], results: dict[str, dict]) -> None:
    # 合并universe元数据 + 拉取结果，过滤出双字段有效的记录
    records: list[dict] = []
    for u in universe:
        r = results.get(u["symbol"], {})
        if not r.get("ok"):
            continue
        rg = r.get("revenue_growth")
        ng = r.get("net_profit_growth")
        if rg is None or ng is None:
            continue
        records.append({
            **u,
            "revenue_growth": rg,
            "net_profit_growth": ng,
            "industry": r.get("industry", ""),
            "pe_ttm": r.get("pe_ttm"),
        })

    total = len(universe)
    fetched = sum(1 for r in results.values() if r.get("ok"))
    data_ok = len(records)

    print(f"\n{'='*65}")
    print(f"  成长门槛探针  {datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')}")
    print(f"{'='*65}")
    print(f"  Universe总数:     {total}")
    print(f"  成功获取数据:     {fetched}  (失败: {len(results)-fetched})")
    print(f"  双字段有效:       {data_ok}")
    print(f"  数据缺失/无效:    {total - data_ok}")

    # ── 门槛灵敏度扫描 ──────────────────────────────────────────────────────
    print(f"\n── 门槛灵敏度（营收同比 AND 净利润同比 均 ≥ X%）──")
    print(f"  {'门槛':>5}  {'通过':>5}  {'占比':>6}  {'加≤200%上限':>10}  {'占比':>6}")
    print("  " + "-" * 50)
    for thresh in [10, 15, 20, 25, 30, 35, 40, 50]:
        p1 = [r for r in records if r["revenue_growth"] >= thresh and r["net_profit_growth"] >= thresh]
        p2 = [r for r in p1 if r["net_profit_growth"] <= 200]
        pct1 = len(p1) / data_ok * 100 if data_ok else 0
        pct2 = len(p2) / data_ok * 100 if data_ok else 0
        marker = "  ← 目标区间 (60-120只)" if 60 <= len(p2) <= 120 else ""
        print(f"  ≥{thresh:>3}%  {len(p1):>5}  {pct1:>5.1f}%  {len(p2):>10}  {pct2:>5.1f}%{marker}")

    # ── 30%门槛通过名单 ─────────────────────────────────────────────────────
    passed = sorted(
        [r for r in records
         if r["revenue_growth"] >= 30
         and r["net_profit_growth"] >= 30
         and r["net_profit_growth"] <= 200],
        key=lambda x: -x["net_profit_growth"],
    )
    print(f"\n── 30%/30%双门槛 + ≤200%上限 通过名单（{len(passed)} 只）──")
    print(f"  {'代码':8} {'名称':10} {'营收同比':>9} {'净利同比':>9} {'PE':>7}  行业")
    print("  " + "-" * 65)
    for r in passed:
        pe_s = f"{r['pe_ttm']:.1f}x" if r["pe_ttm"] else "  N/A"
        ind = (r["industry"] or "未知")[:10]
        print(f"  {r['symbol']:8} {r['name'][:8]:10} {r['revenue_growth']:>8.1f}% {r['net_profit_growth']:>8.1f}% {pe_s:>7}  {ind}")

    # ── 行业分布 ────────────────────────────────────────────────────────────
    if passed:
        ind_cnt: dict[str, int] = {}
        for r in passed:
            ind = (r["industry"] or "未知")
            ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
        print(f"\n── 行业分布（30%门槛通过）──")
        for ind, cnt in sorted(ind_cnt.items(), key=lambda x: -x[1])[:15]:
            bar = "█" * cnt
            print(f"  {ind[:12]:14} {cnt:3}  {bar}")

    # ── 增速分布直方图 ──────────────────────────────────────────────────────
    print(f"\n── 营收同比分布（{data_ok}只有效数据）──")
    buckets = [(-999, -50), (-50, 0), (0, 10), (10, 20), (20, 30),
               (30, 50), (50, 100), (100, 200), (200, 9999)]
    for lo, hi in buckets:
        cnt = sum(1 for r in records if lo <= r["revenue_growth"] < hi)
        hi_s = " +∞ " if hi == 9999 else f"{hi:4}%"
        label = f"[{lo:>4}%, {hi_s})"
        bar = "█" * (cnt // max(data_ok // 60, 1))
        print(f"  {label}  {cnt:4}  {bar}")

    print(f"\n── 净利润同比分布（{data_ok}只有效数据）──")
    for lo, hi in buckets:
        cnt = sum(1 for r in records if lo <= r["net_profit_growth"] < hi)
        hi_s = " +∞ " if hi == 9999 else f"{hi:4}%"
        label = f"[{lo:>4}%, {hi_s})"
        bar = "█" * (cnt // max(data_ok // 60, 1))
        print(f"  {label}  {cnt:4}  {bar}")

    print(f"\n结果已缓存到: {CACHE_JSONL}")
    print("下次可用 --use-cache 跳过拉取直接看分析结果")


# ── 主入口 ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="成长门槛探针")
    parser.add_argument("--sample", type=int, default=0,
                        help="随机抽取N只股票（0=全量，约800只）")
    parser.add_argument("--use-cache", action="store_true",
                        help="直接复用上次JSONL缓存，不重新拉取")
    args = parser.parse_args()

    universe = fetch_universe()

    if args.sample > 0:
        universe = random.sample(universe, min(args.sample, len(universe)))
        print(f"随机抽样: {len(universe)} 只")

    cache = load_cache()

    if args.use_cache:
        print(f"复用缓存: {len(cache)} 条记录，跳过拉取")
    else:
        # 清空旧缓存，重新拉取
        if CACHE_JSONL.exists():
            CACHE_JSONL.unlink()
        cache = {}

        todo = [u for u in universe if u["symbol"] not in cache]
        eta_min = len(todo) * 1.7 / 60
        print(f"开始拉取 {len(todo)} 只基本面数据，预计 {eta_min:.0f}-{eta_min*1.3:.0f} 分钟...\n")

        for i, u in enumerate(todo, 1):
            rec = fetch_one(u["symbol"])
            rec["name"] = u["name"]
            cache[u["symbol"]] = rec
            save_record(rec)

            ok = rec.get("ok", False)
            rg = rec.get("revenue_growth")
            ng = rec.get("net_profit_growth")
            rg_s = f"{rg:+.1f}%" if isinstance(rg, (int, float)) else "  N/A "
            ng_s = f"{ng:+.1f}%" if isinstance(ng, (int, float)) else "  N/A "
            status = "✓" if ok else "✗"
            print(f"[{i:3}/{len(todo)}] {status} {u['symbol']} {u['name'][:8]:8}  "
                  f"Rev={rg_s:>8}  NP={ng_s:>8}", flush=True)

            time.sleep(0.2)

    analyze(universe, cache)


if __name__ == "__main__":
    main()
