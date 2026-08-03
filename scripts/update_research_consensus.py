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
            delta_pct = (latest / frozen - 1) * 100 if (latest is not None and frozen) else None
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
            rec["revenue_yuan"] = fields["revenue"]
        if "profit" in fields:
            rec["profit_yuan"] = fields["profit"]
        disp = dispersion.get(year[:4]) if year.endswith("E") else None
        if disp:
            rec["orgs"] = disp["orgs"]
            rec["profit_min_yuan"] = disp["min"]
            rec["profit_max_yuan"] = disp["max"]
            rec["spread_ratio"] = disp["spread_ratio"]
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


def fetch_ths(symbol: str) -> tuple[dict, dict]:
    """拉同花顺两张表,返回 `(parsed, dispersion)`。akshare 在函数内 import 以便测试免装。"""
    import akshare as ak

    detail = ak.stock_profit_forecast_ths(symbol=symbol, indicator="业绩预测详表-详细指标预测")
    if detail is None or detail.empty:
        raise ValueError("同花顺无业绩预测详表(可能本年度暂无机构预测)")
    parsed = parse_ths_detail(detail.columns.tolist(), detail.astype(str).values.tolist())
    if not parsed:
        raise ValueError("详细指标预测表解析出 0 条金额(上游表结构可能已变)")

    try:
        prof = ak.stock_profit_forecast_ths(symbol=symbol, indicator="预测年报净利润")
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
    skipped_hk: list[str] = []

    for stock in stocks:
        symbol, name = stock["symbol"], stock["name"]
        if stock.get("exchange") == "HK":
            # 同花顺不覆盖港股;券商个体预测表口径不同(无一致均值、无营收),
            # 强行混进同一份 JSON 会让下游误以为口径一致。留人工维护。
            skipped_hk.append(f"{symbol} {name}")
            continue

        try:
            parsed, dispersion = fetch_ths(symbol)
            record = build_record(stock, parsed, dispersion, fetched_at)
            if not args.dry_run:
                write_and_deploy(stock["snapshot_key"], record)

            flagged = significant_changes(record["deltas_vs_registry"], args.threshold)
            flagged_all.extend((name, d) for d in flagged)

            e26 = record["estimates"].get("2026E", {})
            orgs = e26.get("orgs")
            print(
                f"  [{symbol}] ✓ {name}  2026E 营收{_fmt_yi(e26.get('revenue_yuan'))} "
                f"净利{_fmt_yi(e26.get('profit_yuan'))}  机构{orgs if orgs else '—'}家"
                f"{'  ⚠' + str(len(flagged)) + '项显著变动' if flagged else ''}"
            )
        except Exception as e:
            msg = f"[{symbol}] {name} FAILED: {type(e).__name__}: {e}"
            print(f"ERROR: {msg}", file=sys.stderr)
            errors.append(msg)
        time.sleep(1.0)  # 同花顺无官方限频,保守间隔

    if skipped_hk:
        print(f"\n跳过港股(同花顺不覆盖,人工维护): {', '.join(skipped_hk)}")

    if flagged_all:
        print(f"\n=== 显著变动(|Δ| ≥ {args.threshold}%),需人工复核后同步注册表 ===")
        for name, d in flagged_all:
            label = "净利" if d["field"] == "profit" else "营收"
            print(
                f"  {name} {d['year']} {label}: {_fmt_yi(d['frozen'])} → "
                f"{_fmt_yi(d['latest'])}  ({d['delta_pct']:+.1f}%)"
            )

    if errors:
        print(f"\n=== FAILED ({len(errors)}/{len(stocks)}) ===", file=sys.stderr)
        for msg in errors:
            print(f"  {msg}", file=sys.stderr)
        return 1

    print(f"\n=== done ({len(stocks) - len(skipped_hk)} stocks, {len(flagged_all)} flagged) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
