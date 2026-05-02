#!/usr/bin/env python3
"""
读取 growth_gate_probe 缓存 JSONL，生成两个输出：

1. growth-gate-probe.html   — 独立全宽页面，显示 107 只完整名单
2. stock-screener.html 摘要 — 只含 stats + 门槛扫描 + 跳转链接

Usage:
  python3 scripts/generate_probe_html.py
"""
import argparse
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import akshare as ak
import requests

BJT = timezone(timedelta(hours=8))
REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_JSONL  = REPO_DIR / "artifacts" / "growth-gate-probe" / "fundamentals.jsonl"
PRICE_CACHE   = REPO_DIR / "artifacts" / "growth-gate-probe" / "price-history.jsonl"
QUALITY_CACHE = REPO_DIR / "artifacts" / "growth-gate-probe" / "quality-metrics.jsonl"
DOCS_DIR  = Path("/home/ubuntu/docs-site/pages")
PROBE_PAGE = DOCS_DIR / "growth-gate-probe.html"
SCREENER_PAGE = DOCS_DIR / "stock-screener.html"

SECTION_START = "<!-- ===== GROWTH-GATE-PROBE ===== -->"
SECTION_END   = "<!-- ===== /GROWTH-GATE-PROBE ===== -->"
INJECT_BEFORE = "<section id=\"layer1\">"

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

EXTRA_COLS = ",".join([
    "SECURITY_CODE",
    "KCFJCXSYJLRTZ",
    "KCFJCXSYJLR",
    "XSMLL",
    "XSMLL_TB",
    "REPORT_DATE",
])
PROFILE_COLS = ",".join([
    "SECURITY_CODE",
    "BOARD_NAME_2LEVEL",
    "ORG_PROFILE",
])


# ── 数据加载 ─────────────────────────────────────────────────────────────────

def load_universe() -> dict[str, dict]:
    universe: dict[str, dict] = {}
    for idx_code, idx_label in [("000300", "沪深300"), ("000905", "中证500")]:
        try:
            df = ak.index_stock_cons_weight_csindex(symbol=idx_code)
            for _, row in df.iterrows():
                code = str(row["成分券代码"])
                name = str(row["成分券名称"])
                if code in universe:
                    universe[code]["index"] = "两者"
                else:
                    universe[code] = {"name": name, "index": idx_label}
        except Exception as e:
            print(f"  Warning: failed to fetch {idx_code}: {e}")
    return universe


def load_cache() -> list[dict]:
    records = []
    for line in CACHE_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


# ── 股价走势缓存 ──────────────────────────────────────────────────────────────

def load_price_cache() -> dict[str, list[float]]:
    cache: dict[str, list[float]] = {}
    if PRICE_CACHE.exists():
        for line in PRICE_CACHE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rec = json.loads(line)
                cache[rec["symbol"]] = rec["prices"]
    return cache


def _parse_pct(val: object) -> float | None:
    """'19.70%' / '19.70' / False / None → float or None."""
    if val is False or val is None:
        return None
    try:
        return float(str(val).replace("%", "").strip())
    except (ValueError, TypeError):
        return None


_TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _tencent_market(symbol: str) -> str:
    """根据代码前缀判断交易所前缀（腾讯财经格式）。"""
    return "sh" if symbol.startswith("6") else "sz"


def fetch_price_history(symbols: list[str], use_cache: bool) -> dict[str, list[float]]:
    """近一年前复权日线收盘价，腾讯财经 K 线 API（不被 EC2 封锁）。
    返回 {symbol: [close, ...]}。"""
    cache = load_price_cache() if use_cache else {}
    missing = [s for s in symbols if s not in cache]

    if missing:
        end_dt   = datetime.now(BJT)
        start_dt = end_dt - timedelta(days=366)
        end_s    = end_dt.strftime("%Y-%m-%d")
        start_s  = start_dt.strftime("%Y-%m-%d")
        print(f"  拉取股价走势（{len(missing)} 只，腾讯财经，约 {len(missing)*0.6/60:.0f} 分钟）...")

        PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        written: set[str] = set(cache.keys())

        for i, sym in enumerate(missing, 1):
            prices: list[float] = []
            try:
                mkt  = _tencent_market(sym)
                code = f"{mkt}{sym}"
                r = requests.get(
                    _TENCENT_URL,
                    params={"param": f"{code},day,{start_s},{end_s},300,qfq"},
                    headers={"Referer": "https://gu.qq.com/"},
                    timeout=10,
                )
                d = r.json()
                stock_data = d.get("data", {}).get(code, {})
                # 科创板(688xxx)只返回 day，主板/创业板返回 qfqday（前复权）
                rows = stock_data.get("qfqday") or stock_data.get("day", [])
                # 每行格式: [date, open, close, high, low, volume, ...]
                prices = [round(float(row[2]), 3) for row in rows if len(row) >= 3]
            except Exception as e:
                print(f"    {sym} 价格获取失败: {e}")
            cache[sym] = prices
            if sym not in written:
                with PRICE_CACHE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"symbol": sym, "prices": prices},
                                       ensure_ascii=False) + "\n")
                written.add(sym)
            if i % 20 == 0:
                print(f"    {i}/{len(missing)} ...", flush=True)
            time.sleep(0.2)

        ok = sum(1 for s in missing if cache.get(s))
        print(f"  股价数据: {ok}/{len(missing)} 只获取成功")
    else:
        print(f"  股价走势: 全部 {len(symbols)} 只复用缓存")

    return {s: cache.get(s, []) for s in symbols}


def fetch_quality_metrics(symbols: list[str], use_cache: bool) -> dict[str, dict]:
    """ROE(年报)、资产负债率(最新期)、CFO质量(年报 OCF/EPS)，来自同花顺财务摘要。
    一次 THS 调用拿三个字段，只对过门槛的 ~100 只股票调用。"""
    cache: dict[str, dict] = {}
    if use_cache and QUALITY_CACHE.exists():
        for line in QUALITY_CACHE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                cache[rec["symbol"]] = rec

    missing = [s for s in symbols if s not in cache]
    if missing:
        print(f"  拉取质量指标（{len(missing)} 只，THS，约 {len(missing)*0.4/60:.0f} 分钟）...")
        QUALITY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        written: set[str] = set(cache.keys())

        for i, sym in enumerate(missing, 1):
            rec: dict = {"symbol": sym, "roe_annual": None, "debt_ratio": None, "cfo_quality": None}
            try:
                df = ak.stock_financial_abstract_ths(symbol=sym, indicator="按报告期")
                df = df.sort_values("报告期", ascending=False).reset_index(drop=True)
                if not df.empty:
                    # 资产负债率：取最新期
                    rec["debt_ratio"] = _parse_pct(df.iloc[0].get("资产负债率"))
                    # ROE 和 CFO质量：取最新年报（报告期以 -12-31 结尾）
                    annual = df[df["报告期"].str.endswith("-12-31")]
                    if not annual.empty:
                        row = annual.iloc[0]
                        rec["roe_annual"] = _parse_pct(row.get("净资产收益率"))
                        ocf = _parse_pct(row.get("每股经营现金流"))
                        eps = _parse_pct(row.get("基本每股收益"))
                        if ocf is not None and eps is not None and abs(eps) > 0.001:
                            rec["cfo_quality"] = round(ocf / eps, 2)
            except Exception as e:
                print(f"    {sym} 质量指标失败: {e}")

            cache[sym] = rec
            if sym not in written:
                with QUALITY_CACHE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written.add(sym)
            if i % 20 == 0:
                print(f"    {i}/{len(missing)} ...", flush=True)
            time.sleep(0.35)

        ok = sum(1 for s in missing if cache.get(s, {}).get("roe_annual") is not None)
        print(f"  质量指标: {ok}/{len(missing)} 只获取成功")
    else:
        print(f"  质量指标: 全部 {len(symbols)} 只复用缓存")

    return {s: cache.get(s, {}) for s in symbols}


def make_sparkline(prices: list[float], width: int = 104, height: int = 38) -> str:
    """生成内嵌 SVG 迷你走势图；价格为空则返回占位符。"""
    if len(prices) < 10:
        return '<span style="color:#555;font-size:11px;">—</span>'

    lo, hi = min(prices), max(prices)
    rng = hi - lo or lo * 0.01 or 1.0

    pad_x, pad_y = 2, 3
    n = len(prices)
    pts = []
    for i, p in enumerate(prices):
        x = pad_x + (width  - 2 * pad_x) * i / (n - 1)
        y = (height - pad_y) - (height - 2 * pad_y) * (p - lo) / rng
        pts.append(f"{x:.1f},{y:.1f}")

    color      = "#3fb950" if prices[-1] >= prices[0] else "#e05252"
    fill_color = color + "28"
    pts_str    = " ".join(pts)

    # 封闭多边形（填充区域）
    bx0 = f"{pad_x:.1f}"
    bxn = f"{pad_x + (width - 2*pad_x):.1f}"
    by  = f"{height - pad_y:.1f}"
    fill_pts = f"{bx0},{by} {pts_str} {bxn},{by}"

    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="display:block;flex-shrink:0;">'
        f'<polygon points="{fill_pts}" fill="{fill_color}" stroke="none"/>'
        f'<polyline points="{pts_str}" stroke="{color}" stroke-width="1.5" '
        f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )


def _batch_fetch(
    symbols: list[str], report_name: str, cols: str,
    batch_size: int = 50, sort: bool = True,
) -> list[dict]:
    rows_all: list[dict] = []
    batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    for batch in batches:
        codes_str = ",".join(f'"{s}"' for s in batch)
        params: dict = {
            "reportName": report_name,
            "columns": cols,
            "filter": f"(SECURITY_CODE in ({codes_str}))",
            "pageSize": len(batch),
        }
        if sort:
            params["sortColumns"] = "REPORT_DATE"
            params["sortTypes"] = "-1"
        try:
            r = requests.get(DC_URL, params=params, headers=DC_HEADERS, timeout=20)
            d = r.json()
            if d.get("success") and d.get("result"):
                rows_all.extend(d["result"]["data"])
        except Exception as e:
            print(f"  batch error: {e}")
        time.sleep(0.3)
    return rows_all


def fetch_extra_fields(symbols: list[str]) -> dict[str, dict]:
    print(f"  拉取扣非净利润（{len(symbols)} 只，{-(-len(symbols)//50)} 批次）...")
    result: dict[str, dict] = {s: {} for s in symbols}
    seen: set[str] = set()
    for row in _batch_fetch(symbols, "RPT_F10_FINANCE_MAINFINADATA", EXTRA_COLS):
        code = row.get("SECURITY_CODE", "")
        if code in result and code not in seen:
            seen.add(code)
            result[code] = {
                "deduct_np_yoy":    row.get("KCFJCXSYJLRTZ"),
                "deduct_np":        row.get("KCFJCXSYJLR"),
                "gross_margin":     row.get("XSMLL"),
                "gross_margin_chg": row.get("XSMLL_TB"),
            }
    print(f"  扣非数据: {sum(1 for v in result.values() if v)}/{len(symbols)} 只")
    return result


def fetch_profile_fields(symbols: list[str]) -> dict[str, dict]:
    print(f"  拉取行业+简介（{len(symbols)} 只，{-(-len(symbols)//50)} 批次）...")
    result: dict[str, dict] = {s: {} for s in symbols}
    for row in _batch_fetch(symbols, "RPT_F10_ORG_BASICINFO", PROFILE_COLS, batch_size=50, sort=False):
        code = row.get("SECURITY_CODE", "")
        if code in result:
            profile = (row.get("ORG_PROFILE") or "").strip()
            result[code] = {
                "industry": row.get("BOARD_NAME_2LEVEL") or "—",
                "profile":  profile[:70] if profile else "—",
            }
    print(f"  行业简介: {sum(1 for v in result.values() if v)}/{len(symbols)} 只")
    return result


# ── 筛选逻辑 ─────────────────────────────────────────────────────────────────

def passes_gate(periods: list[dict], thresh: float) -> bool:
    if not periods:
        return False
    p = periods[0]
    rg, ng, np_val = p.get("revenue_growth"), p.get("net_profit_growth"), p.get("net_profit")
    if rg is None or ng is None:
        return False
    if np_val is not None and np_val <= 0:
        return False
    return rg >= thresh and ng >= thresh


def passes_continuity(periods: list[dict], min_thresh: float, n: int) -> bool:
    if len(periods) < n:
        return False
    for p in periods[:n]:
        rg = p.get("revenue_growth")
        ng = p.get("net_profit_growth")
        if rg is None or ng is None or rg < min_thresh or ng < min_thresh:
            return False
    return True


def classify_growth(rg: float, ng: float, extra: dict, periods_data: list[dict]) -> str:
    tags = []
    dng = extra.get("deduct_np_yoy")
    gm_chg = extra.get("gross_margin_chg")
    if dng is not None:
        gap = ng - dng
        if gap > 50:
            tags.append("⚠ 非经常性收益主导")
        elif gap > 20:
            tags.append("非经常性收益显著")
        elif gap < -15:
            tags.append("非经常性损失拖累")
        if dng >= 30:
            ratio = dng / max(abs(rg), 1)
            if ratio > 1.4:
                tags.append("利润率扩张")
            elif ratio < 0.6:
                tags.append("规模增长为主")
            else:
                tags.append("主业稳健增长")
        elif 0 <= dng < 30:
            tags.append("扣非增速较温和")
        else:
            tags.append("扣非承压")
    else:
        tags.append("扣非数据缺失")
    if len(periods_data) >= 2:
        prev_ng = periods_data[1].get("net_profit_growth")
        prev_rg = periods_data[1].get("revenue_growth")
        if prev_ng is not None and ng - prev_ng > 30:
            tags.append("业绩加速↑")
        if prev_rg is not None and rg - prev_rg > 20:
            tags.append("营收提速")
    if gm_chg is not None:
        if gm_chg > 3:
            tags.append("毛利率↑")
        elif gm_chg < -3:
            tags.append("毛利率↓")
    return " · ".join(tags) if tags else "主业增长"


def bar(count: int, scale: int, color: str = "#17becf") -> str:
    width = min(100, max(0, count * 100 // max(scale, 1)))
    return (f'<div class="dist-bar-wrap">'
            f'<div class="dist-bar" style="width:{width}%;background:{color}"></div>'
            f'<span class="dist-cnt">{count}</span></div>')


# ── 行渲染 ───────────────────────────────────────────────────────────────────

def stock_row_html(r: dict, universe: dict, extra_map: dict,
                   profile_map: dict, price_map: dict, quality_map: dict) -> str:
    p0  = r["periods"][0]
    sym = r["symbol"]
    rg  = p0["revenue_growth"]
    ng  = p0["net_profit_growth"]

    info  = universe.get(sym, {})
    name  = info.get("name", "")[:6]
    index = info.get("index", "—")
    idx_color = {"沪深300": "#17becf", "中证500": "#3fb950", "两者": "#ffd700"}.get(index, "#8b949e")

    extra = extra_map.get(sym, {})
    dng   = extra.get("deduct_np_yoy")
    if dng is not None:
        dng_s = f"{dng:+.1f}%"
        dng_color = "#e3b341" if (ng - dng) > 20 else ("#3fb950" if dng >= 30 else "#8b949e")
    else:
        dng_s, dng_color = "—", "#8b949e"

    cont       = "✓" if passes_continuity(r["periods"], 15, 2) else "—"
    cont_color = "#3fb950" if cont == "✓" else "#8b949e"
    analysis   = classify_growth(rg, ng, extra, r["periods"])

    pinfo    = profile_map.get(sym, {})
    industry = pinfo.get("industry", "—")
    profile  = pinfo.get("profile", "—")

    sparkline = make_sparkline(price_map.get(sym, []))

    qm = quality_map.get(sym, {})
    roe    = qm.get("roe_annual")
    debt   = qm.get("debt_ratio")
    cfo_q  = qm.get("cfo_quality")

    roe_s    = f"{roe:.1f}%" if roe is not None else "—"
    roe_col  = ("#3fb950" if roe >= 15 else "#8b949e") if roe is not None else "#555"
    debt_s   = f"{debt:.1f}%" if debt is not None else "—"
    debt_col = ("#e05252" if debt > 65 else "#8b949e") if debt is not None else "#555"
    cfo_s    = f"{cfo_q:.2f}" if cfo_q is not None else "—"
    cfo_col  = ("#3fb950" if cfo_q >= 0.8 else ("#e3b341" if cfo_q >= 0 else "#e05252")) if cfo_q is not None else "#555"

    return (
        f'<tr>'
        f'<td class="mono">{sym}</td>'
        f'<td class="nowrap">{name}</td>'
        f'<td class="sparkline-cell">{sparkline}</td>'
        f'<td class="ctr"><span style="color:{idx_color};font-size:11px;">{index}</span></td>'
        f'<td class="ctr industry-cell">{industry}</td>'
        f'<td class="profile-cell">{profile}</td>'
        f'<td class="num" style="color:#17becf;">{rg:+.1f}%</td>'
        f'<td class="num" style="color:#9ecde6;">{ng:+.1f}%</td>'
        f'<td class="num" style="color:{dng_color};font-weight:600;">{dng_s}</td>'
        f'<td class="num" style="color:{roe_col};">{roe_s}</td>'
        f'<td class="num" style="color:{debt_col};">{debt_s}</td>'
        f'<td class="num" style="color:{cfo_col};">{cfo_s}</td>'
        f'<td class="ctr" style="color:{cont_color};">{cont}</td>'
        f'<td class="analysis-cell">{analysis}</td>'
        f'<td class="nowrap muted">{p0.get("period_name","")}</td>'
        f'</tr>\n'
    )


# ── 独立页面生成 ──────────────────────────────────────────────────────────────

def generate_probe_page(
    passed_30: list[dict],
    universe: dict,
    extra_map: dict,
    profile_map: dict,
    price_map: dict,
    quality_map: dict,
    sweep: list[tuple],
    cont2: list,
    cont3: list,
    data_ok: int,
    period_name: str,
    report_date: str,
    run_ts: str,
) -> str:
    cnt = len(passed_30)

    # 门槛扫描 rows
    sweep_rows = ""
    for th, cnt_th, pct, target in sweep:
        hl = ' class="hl-row"' if target else ""
        tag = ""
        if target:
            tag = '<span class="badge-target">目标区间</span>'
        elif cnt_th < 30:
            tag = '<span class="muted-sm">↑ 候选池过稀</span>'
        elif cnt_th > 200:
            tag = '<span class="muted-sm">↓ 候选池过密</span>'
        sweep_rows += (
            f'<tr{hl}><td>≥ {th}%</td>'
            f'<td class="num">{cnt_th}</td>'
            f'<td class="num">{pct:.1f}%</td>'
            f'<td>{tag}</td></tr>\n'
        )

    # 107只名单
    all_rows = "".join(
        stock_row_html(r, universe, extra_map, profile_map, price_map, quality_map) for r in passed_30
    )

    # 分布直方图
    buckets = [(-999, -50), (-50, 0), (0, 10), (10, 20), (20, 30),
               (30, 50), (50, 100), (100, 200), (200, 9999)]
    all_records_with_data = [r for r in passed_30]  # already filtered

    pct_cont2 = len(cont2) / cnt * 100 if cnt else 0
    pct_cont3 = len(cont3) / cnt * 100 if cnt else 0

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>成长门槛探针 — {cnt}只高增长股</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="stylesheet" href="/css/components.css">
<style>
:root {{
  --page-accent: #17becf;
  --accent6: #17becf;
}}
body {{ background: var(--bg); color: var(--text); font-family: var(--font); }}
.page-wrap {{ max-width: 1480px; margin: 0 auto; padding: 24px 28px 80px; }}

/* header */
.page-header {{ display:flex; align-items:baseline; gap:16px; margin-bottom:24px; flex-wrap:wrap; }}
.page-header h1 {{ font-size:22px; font-weight:700;
  background:linear-gradient(135deg,#17becf,#0d8a96);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }}
.back-link {{ color:var(--text-muted); font-size:13px; text-decoration:none; }}
.back-link:hover {{ color:var(--text); }}
.run-ts {{ color:var(--text-muted); font-size:12px; margin-left:auto; }}

/* stats grid */
.stats-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:20px 0; }}
.stat-card {{ background:var(--surface); border:1px solid var(--border); border-radius:8px;
  padding:14px 18px; }}
.stat-card .value {{ font-size:26px; font-weight:700; color:var(--accent6); }}
.stat-card .label {{ font-size:11px; color:var(--text-muted); text-transform:uppercase; margin-top:4px; }}

/* callout */
.callout {{ background:var(--surface); border-left:3px solid var(--accent6);
  border-radius:0 6px 6px 0; padding:10px 14px; font-size:13px; margin:16px 0; }}

/* section headings */
h2 {{ font-size:17px; font-weight:600; margin:32px 0 12px; border-bottom:1px solid var(--border);
  padding-bottom:6px; color:var(--accent6); }}
h3 {{ font-size:14px; font-weight:600; margin:20px 0 8px; color:var(--text); }}

/* tables — shared */
table {{ border-collapse:collapse; width:100%; }}
th, td {{ padding:6px 10px; text-align:left; font-size:12.5px; border-bottom:1px solid var(--border); }}
th {{ color:var(--text-muted); font-weight:500; white-space:nowrap; background:var(--surface); position:sticky; top:0; z-index:2; cursor:pointer; }}
th .sort-icon {{ font-size:10px; opacity:0.3; margin-left:3px; user-select:none; }}
th:hover .sort-icon {{ opacity:0.7; }}
th.sort-active {{ color:var(--accent6); }}
th.sort-active .sort-icon {{ opacity:1; color:var(--accent6); }}
tr:hover td {{ background:var(--surface2); }}
.hl-row td {{ background:#17becf0d; }}

/* stock table helpers */
.mono {{ font-family:monospace; font-size:12px; white-space:nowrap; }}
.nowrap {{ white-space:nowrap; }}
.ctr {{ text-align:center; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.muted {{ color:var(--text-muted); }}
.muted-sm {{ font-size:11px; color:var(--text-muted); }}
.industry-cell {{ white-space:nowrap; font-size:11px; }}
.profile-cell {{ font-size:11px; color:var(--text-muted); max-width:260px; }}
.analysis-cell {{ font-size:11px; color:var(--text-muted); min-width:160px; }}
.sparkline-cell {{ padding:4px 8px; vertical-align:middle; }}
.badge-target {{ background:#17becf22; color:#17becf; font-size:10px;
  padding:2px 6px; border-radius:4px; white-space:nowrap; }}

/* scrollable table container */
.table-scroll {{ overflow-x:auto; border:1px solid var(--border); border-radius:8px; }}
.table-scroll.tall {{ max-height:75vh; overflow-y:auto; }}

/* continuity mini-table */
.cont-table td {{ border-bottom:1px solid var(--border); padding:8px 12px; }}

/* dist bar */
.dist-bar-wrap {{ display:flex; align-items:center; gap:6px; width:100%; }}
.dist-bar {{ height:13px; border-radius:3px; min-width:2px; }}
.dist-cnt {{ font-size:11px; color:var(--text-muted); white-space:nowrap; }}

/* filter bar */
.filter-bar {{
  display:flex; align-items:center; gap:10px 14px; flex-wrap:wrap;
  background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:10px 14px; margin:10px 0 8px;
  font-size:12.5px;
}}
.filter-label {{ font-weight:600; color:var(--text-muted); white-space:nowrap; margin-right:4px; }}
.filter-item {{ display:flex; align-items:center; gap:4px; white-space:nowrap; color:var(--text-muted); }}
.filter-item input {{
  background:var(--bg); border:1px solid var(--border); border-radius:4px;
  color:var(--text); padding:3px 7px; width:62px; font-size:12px;
}}
.filter-item input[type="text"] {{ width:84px; }}
.filter-item input:focus {{ outline:none; border-color:var(--accent6); box-shadow:0 0 0 2px #17becf22; }}
.filter-reset {{
  background:#17becf1a; border:1px solid #17becf44; color:#17becf;
  border-radius:4px; padding:3px 12px; cursor:pointer; font-size:12px; white-space:nowrap;
}}
.filter-reset:hover {{ background:#17becf33; }}
.filter-count {{ margin-left:auto; color:var(--accent6); font-weight:700; white-space:nowrap; font-size:13px; }}

/* responsive */
@media(max-width:900px) {{
  .stats-grid {{ grid-template-columns:repeat(2,1fr); }}
  .page-wrap {{ padding:16px; }}
}}
</style>
</head>
<body>
<div class="page-wrap">

<div class="page-header">
  <a class="back-link" href="/stock-screener.html">← 返回选股设计</a>
  <h1>成长门槛探针</h1>
  <span class="run-ts">{run_ts} BJT &nbsp;|&nbsp; 数据: {period_name}（{report_date}）</span>
</div>

<div class="callout">
  对 <strong>沪深300 + 中证500</strong> 全量 800 只 A 股拉取近 3 期财报，筛选
  <strong>营收同比 AND 扣非净利润同比均 ≥ 30%</strong> 且本期净利润 &gt; 0 的高增长标的。
  行业分类来自东方财富（结构近似申万二级）。
</div>

<!-- stats -->
<div class="stats-grid">
  <div class="stat-card">
    <div class="value">800</div>
    <div class="label">Universe（沪深300+中证500）</div>
  </div>
  <div class="stat-card">
    <div class="value">{cnt}</div>
    <div class="label">过双≥30%门槛</div>
  </div>
  <div class="stat-card">
    <div class="value">{cnt/data_ok*100:.1f}%</div>
    <div class="label">入选率</div>
  </div>
  <div class="stat-card">
    <div class="value">{len(cont2)}</div>
    <div class="label">近2期连续≥15%</div>
  </div>
</div>

<!-- threshold sweep -->
<h2>门槛灵敏度</h2>
<p style="font-size:12px;color:var(--text-muted);margin-bottom:10px;">
  营收同比 AND 净利润同比均 ≥ X%，且本期净利润 &gt; 0
</p>
<div class="table-scroll" style="max-width:480px;">
<table>
  <thead><tr>
    <th>门槛</th><th class="num">通过</th><th class="num">占比</th><th>备注</th>
  </tr></thead>
  <tbody>{sweep_rows}</tbody>
</table>
</div>

<!-- continuity -->
<h2>连续性检验</h2>
<div class="table-scroll" style="max-width:480px;">
<table class="cont-table">
  <tr>
    <td>最新期双≥30%（本期盈利）</td>
    <td class="num" style="font-weight:600;">{cnt} 只</td>
    <td class="muted-sm">baseline</td>
  </tr>
  <tr>
    <td>+ 近2期均≥15%</td>
    <td class="num" style="font-weight:600;">{len(cont2)} 只</td>
    <td class="muted-sm">淘汰 {100-pct_cont2:.0f}%</td>
  </tr>
  <tr>
    <td>+ 近3期均≥15%</td>
    <td class="num" style="font-weight:600;">{len(cont3)} 只</td>
    <td class="muted-sm">淘汰 {100-pct_cont3:.0f}%</td>
  </tr>
</table>
</div>

<!-- full stock table -->
<h2>完整名单 — {cnt} 只（按净利润同比降序）</h2>
<div class="callout" style="font-size:12px;">
  <strong>列说明</strong>：
  指数 <span style="color:#17becf;">■</span>沪深300
  <span style="color:#3fb950;">■</span>中证500
  <span style="color:#ffd700;">■</span>两者 &nbsp;|&nbsp;
  净利同比 = 含非经常性损益 &nbsp;|&nbsp;
  <strong>扣非净利同比</strong> = 扣除非经常性损益（橙色 = 与净利差距 &gt;20pp，净利润有一次性收益撑高）&nbsp;|&nbsp;
  2期 ✓ = 近2期营收+净利均≥15%
</div>
<div class="filter-bar" id="filter-bar">
  <span class="filter-label">筛选</span>
  <label class="filter-item">行业
    <input class="filter-input" type="text" data-col="4" data-op="text" placeholder="关键字">
  </label>
  <label class="filter-item">营收同比 ≥
    <input class="filter-input" type="number" data-col="6" data-op="min" placeholder="30">%
  </label>
  <label class="filter-item">净利同比 ≥
    <input class="filter-input" type="number" data-col="7" data-op="min" placeholder="30">%
  </label>
  <label class="filter-item">ROE年报 ≥
    <input class="filter-input" type="number" data-col="9" data-op="min" placeholder="15">%
  </label>
  <label class="filter-item">资产负债率 ≤
    <input class="filter-input" type="number" data-col="10" data-op="max" placeholder="65">%
  </label>
  <label class="filter-item">CFO质量 ≥
    <input class="filter-input" type="number" data-col="11" data-op="min" placeholder="0.8">
  </label>
  <button class="filter-reset" onclick="resetFilters()">重置</button>
  <span class="filter-count" id="filter-count">{cnt} / {cnt} 只</span>
</div>
<div class="table-scroll tall">
<table id="stock-table">
  <thead><tr>
    <th>代码</th>
    <th>名称</th>
    <th data-no-sort>走势</th>
    <th class="ctr">指数</th>
    <th class="ctr">行业</th>
    <th>主营简介</th>
    <th class="num">营收同比</th>
    <th class="num">净利同比</th>
    <th class="num">扣非净利同比</th>
    <th class="num">ROE年报</th>
    <th class="num">资产负债率</th>
    <th class="num">CFO质量</th>
    <th class="ctr">2期</th>
    <th>增速分析</th>
    <th>报告期</th>
  </tr></thead>
  <tbody>
{all_rows}  </tbody>
</table>
</div>

<p style="font-size:12px;color:var(--text-muted);margin-top:20px;">
  脚本: <code>scripts/growth_gate_probe.py</code> +
  <code>scripts/generate_probe_html.py</code> &nbsp;|&nbsp;
  数据: datacenter-web.eastmoney.com &nbsp;|&nbsp;
  行业字段: <code>BOARD_NAME_2LEVEL</code>（RPT_F10_ORG_BASICINFO，近似申万二级）
</p>

</div>
<script src="/js/sync-highlight.js" defer></script>
<script>
(function() {{
  var table = document.getElementById('stock-table');
  if (!table) return;
  var tbody = table.querySelector('tbody');
  var ths = Array.from(table.querySelectorAll('thead th'));
  var sortState = {{col: -1, asc: true}};

  function parseVal(cell) {{
    var t = cell.textContent.trim();
    if (t === '—' || t === '') return null;
    if (t === '✓') return 1;
    var n = parseFloat(t.replace(/[+%,\\s]/g, ''));
    if (!isNaN(n)) return n;
    return t;
  }}

  function compare(a, b, asc) {{
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    if (typeof a === 'number' && typeof b === 'number') return asc ? a - b : b - a;
    return asc ? String(a).localeCompare(String(b), 'zh-CN') : String(b).localeCompare(String(a), 'zh-CN');
  }}

  ths.forEach(function(th, ci) {{
    if (th.hasAttribute('data-no-sort')) return;
    var icon = document.createElement('span');
    icon.className = 'sort-icon';
    icon.textContent = '⇅';
    th.appendChild(icon);
    th.addEventListener('click', function() {{
      var asc = sortState.col === ci ? !sortState.asc : true;
      sortState = {{col: ci, asc: asc}};
      ths.forEach(function(h) {{
        h.querySelector('.sort-icon').textContent = '⇅';
        h.classList.remove('sort-active');
      }});
      th.querySelector('.sort-icon').textContent = asc ? '▲' : '▼';
      th.classList.add('sort-active');
      var rows = Array.from(tbody.rows);
      rows.sort(function(ra, rb) {{
        return compare(parseVal(ra.cells[ci]), parseVal(rb.cells[ci]), asc);
      }});
      rows.forEach(function(r) {{ tbody.appendChild(r); }});
    }});
  }});
}})();

// ── 列筛选 ────────────────────────────────────────────────────────────────────
function applyFilters() {{
  var inputs  = document.querySelectorAll('.filter-input');
  var active  = [];
  inputs.forEach(function(inp) {{
    var v = inp.value.trim();
    if (v) active.push({{ col: parseInt(inp.dataset.col), op: inp.dataset.op || 'min', val: v }});
  }});
  var rows   = document.querySelectorAll('#stock-table tbody tr');
  var shown  = 0;
  rows.forEach(function(row) {{
    var pass = true;
    for (var fi = 0; fi < active.length; fi++) {{
      var f    = active[fi];
      var cell = row.cells[f.col];
      if (!cell) {{ pass = false; break; }}
      var text = cell.textContent.trim();
      if (f.op === 'text') {{
        if (!text.toLowerCase().includes(f.val.toLowerCase())) {{ pass = false; break; }}
      }} else {{
        if (text === '—' || text === '') {{ pass = false; break; }}
        var num = parseFloat(text.replace(/[+%,\\s]/g, ''));
        var thr = parseFloat(f.val);
        if (isNaN(num) || isNaN(thr)) continue;
        if (f.op === 'min' && num < thr) {{ pass = false; break; }}
        if (f.op === 'max' && num > thr) {{ pass = false; break; }}
      }}
    }}
    row.style.display = pass ? '' : 'none';
    if (pass) shown++;
  }});
  var el = document.getElementById('filter-count');
  if (el) el.textContent = shown + ' / {cnt} 只';
}}

function resetFilters() {{
  document.querySelectorAll('.filter-input').forEach(function(inp) {{ inp.value = ''; }});
  document.querySelectorAll('#stock-table tbody tr').forEach(function(r) {{ r.style.display = ''; }});
  var el = document.getElementById('filter-count');
  if (el) el.textContent = '{cnt} / {cnt} 只';
}}

document.querySelectorAll('.filter-input').forEach(function(inp) {{
  inp.addEventListener('input', applyFilters);
}});
</script>
</body>
</html>"""


# ── stock-screener.html 摘要节 ────────────────────────────────────────────────

def generate_screener_section(
    cnt_30: int,
    data_ok: int,
    sweep: list[tuple],
    cont2: list,
    cont3: list,
    period_name: str,
    report_date: str,
    run_ts: str,
) -> str:
    sweep_rows = ""
    for th, cnt_th, pct, target in sweep:
        hl = ' style="background:#17becf0d;"' if target else ""
        tag = ""
        if target:
            tag = '<span class="badge" style="background:#17becf22;color:#17becf;font-size:10px;margin-left:6px;">目标区间</span>'
        elif cnt_th < 30:
            tag = '<span style="font-size:11px;color:var(--text-muted);margin-left:4px;">↑ 过稀</span>'
        elif cnt_th > 200:
            tag = '<span style="font-size:11px;color:var(--text-muted);margin-left:4px;">↓ 过密</span>'
        sweep_rows += (
            f'<tr{hl}>'
            f'<td>≥ {th}%</td>'
            f'<td style="text-align:right;">{cnt_th}</td>'
            f'<td style="text-align:right;">{pct:.1f}%</td>'
            f'<td><span class="zh">{tag}</span></td>'
            f'</tr>\n'
        )

    pct_cont2 = len(cont2) / cnt_30 * 100 if cnt_30 else 0
    pct_cont3 = len(cont3) / cnt_30 * 100 if cnt_30 else 0

    return f"""
{SECTION_START}
<section id="growth-gate-probe" class="updated-latest">
  <h2>
    <span class="num" style="font-size:10px;">6.5</span>
    <span class="zh">成长门槛探针</span><span class="en">Growth Gate Probe</span>
    <span class="section-ts">{run_ts} BJT</span>
  </h2>

  <p>
    <span class="zh">对沪深300 + 中证500全量 800 只 A 股拉取近 3 期财报，分析不同增速门槛下的候选池密度，
    验证 <strong>双 ≥30% 门槛</strong> 是否落在 60–120 只目标区间，并检验业绩连续性淘汰率。
    完整名单（{cnt_30} 只，含行业 / 主营简介 / 扣非净利润分析）见专页。</span>
    <span class="en">Threshold sensitivity analysis across 800 CSI 300+500 A-shares.
    Full passing list ({cnt_30} stocks with industry, business description, and adj NP YoY) on the dedicated page.</span>
  </p>

  <!-- stats cards -->
  <div class="stats-grid" style="margin-top:16px;">
    <div class="stat-card">
      <div class="value">800</div>
      <div class="label"><span class="zh">Universe</span><span class="en">Universe</span></div>
    </div>
    <div class="stat-card">
      <div class="value">{cnt_30}</div>
      <div class="label"><span class="zh">过双≥30%门槛</span><span class="en">Pass dual ≥30%</span></div>
    </div>
    <div class="stat-card">
      <div class="value">{cnt_30/data_ok*100:.1f}%</div>
      <div class="label"><span class="zh">入选率</span><span class="en">Selection rate</span></div>
    </div>
    <div class="stat-card">
      <div class="value">{len(cont2)}</div>
      <div class="label"><span class="zh">2期连续≥15%</span><span class="en">2-period ≥15%</span></div>
    </div>
  </div>

  <div class="callout" style="margin-top:16px;">
    <span class="zh">数据报告期：<strong>{period_name}</strong>（{report_date}）。
    门槛：营收同比 AND 净利润同比均≥30%，且本期净利润&gt;0（排除由亏转盈基数效应）。</span>
    <span class="en">Data as of: <strong>{period_name}</strong> ({report_date}).
    Gate: Rev YoY AND NP YoY ≥30%, current NP &gt; 0 (excludes loss→profit base distortion).</span>
  </div>

  <!-- threshold sweep -->
  <h3 style="margin-top:20px;"><span class="zh">门槛灵敏度</span><span class="en">Threshold Sensitivity</span></h3>
  <table>
    <thead><tr>
      <th><span class="zh">门槛</span><span class="en">Threshold</span></th>
      <th style="text-align:right;"><span class="zh">通过</span><span class="en">Pass</span></th>
      <th style="text-align:right;"><span class="zh">占比</span><span class="en">Pct</span></th>
      <th></th>
    </tr></thead>
    <tbody>{sweep_rows}</tbody>
  </table>

  <!-- continuity -->
  <h3 style="margin-top:20px;"><span class="zh">连续性检验</span><span class="en">Continuity</span></h3>
  <div class="card" style="padding:12px 16px;">
    <table style="margin:0;">
      <tr>
        <td><span class="zh">最新期双≥30%（本期盈利）</span><span class="en">Latest dual ≥30%</span></td>
        <td style="text-align:right;font-weight:600;">{cnt_30} 只</td>
        <td style="font-size:11px;color:var(--text-muted);">baseline</td>
      </tr>
      <tr>
        <td><span class="zh">+ 近2期均≥15%</span><span class="en">+ Prior 2 periods ≥15%</span></td>
        <td style="text-align:right;font-weight:600;">{len(cont2)} 只</td>
        <td style="font-size:11px;color:var(--text-muted);"><span class="zh">淘汰 {100-pct_cont2:.0f}%</span><span class="en">Attrition {100-pct_cont2:.0f}%</span></td>
      </tr>
      <tr>
        <td><span class="zh">+ 近3期均≥15%</span><span class="en">+ Prior 3 periods ≥15%</span></td>
        <td style="text-align:right;font-weight:600;">{len(cont3)} 只</td>
        <td style="font-size:11px;color:var(--text-muted);"><span class="zh">淘汰 {100-pct_cont3:.0f}%</span><span class="en">Attrition {100-pct_cont3:.0f}%</span></td>
      </tr>
    </table>
  </div>

  <!-- link to full page -->
  <div style="margin-top:20px;text-align:center;">
    <a href="/growth-gate-probe.html"
       style="display:inline-block;background:#17becf;color:#000;font-weight:600;
              font-size:13px;padding:10px 28px;border-radius:6px;text-decoration:none;">
      <span class="zh">查看完整名单 → {cnt_30} 只详细分析</span>
      <span class="en">View Full List → {cnt_30} stocks with detail</span>
    </a>
  </div>

  <p style="font-size:12px;color:var(--text-muted);margin-top:12px;">
    <span class="zh">脚本: <code>scripts/growth_gate_probe.py</code> | 数据: datacenter-web.eastmoney.com</span>
    <span class="en">Script: <code>scripts/growth_gate_probe.py</code> | Source: datacenter-web.eastmoney.com</span>
  </p>
</section>
{SECTION_END}
"""


# ── nav link ─────────────────────────────────────────────────────────────────

def add_nav_link(html: str) -> str:
    marker = 'href="#growth-gate-probe"'
    if marker in html:
        return html
    after = 'href="#phase0-results"'
    idx = html.index(after)
    line_end = html.index("\n", idx) + 1
    new_link = ('    <a href="#growth-gate-probe" style="padding-left:32px;font-size:12px;">'
                '<span class="zh">↳ 成长门槛探针</span>'
                '<span class="en">↳ Growth Gate Probe</span>'
                ' <span class="badge" style="background:#ffd70033;color:#ffd700;">NEW</span></a>\n')
    return html[:line_end] + new_link + html[line_end:]


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="生成 growth-gate-probe HTML 报告")
    parser.add_argument("--use-price-cache", action="store_true",
                        help="复用上次股价缓存，跳过腾讯财经拉取（~1min）")
    parser.add_argument("--use-quality-cache", action="store_true",
                        help="复用上次质量指标缓存，跳过 THS 拉取（~1min）")
    args = parser.parse_args()

    run_ts = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")

    print("Fetching universe...")
    universe = load_universe()
    print(f"  {len(universe)} stocks")

    print("Loading cache...")
    records = load_cache()
    print(f"  {len(records)} records")

    data_ok_list = [
        r for r in records
        if r["periods"] and
        r["periods"][0].get("revenue_growth") is not None and
        r["periods"][0].get("net_profit_growth") is not None
    ]
    data_ok = len(data_ok_list)
    period_name = data_ok_list[0]["periods"][0]["period_name"] if data_ok_list else "—"
    report_date = data_ok_list[0]["periods"][0]["report_date"] if data_ok_list else "—"

    # 门槛扫描
    thresholds = [10, 15, 20, 25, 30, 35, 40, 50]
    sweep = []
    for th in thresholds:
        passed = [r for r in data_ok_list if passes_gate(r["periods"], th)]
        pct = len(passed) / data_ok * 100 if data_ok else 0
        sweep.append((th, len(passed), pct, (60 <= len(passed) <= 120)))

    passed_30_raw = [r for r in data_ok_list if passes_gate(r["periods"], 30)]
    passed_30 = sorted(passed_30_raw, key=lambda x: -x["periods"][0]["net_profit_growth"])
    cnt_30 = len(passed_30)

    cont2 = [r for r in passed_30_raw if passes_continuity(r["periods"], 15, 2)]
    cont3 = [r for r in passed_30_raw if passes_continuity(r["periods"], 15, 3)]

    passing_symbols = [r["symbol"] for r in passed_30]

    print(f"\n过门槛: {cnt_30} 只，拉取额外字段...")
    extra_map   = fetch_extra_fields(passing_symbols)
    profile_map = fetch_profile_fields(passing_symbols)

    print("\n拉取股价走势...")
    price_map = fetch_price_history(passing_symbols, use_cache=args.use_price_cache)

    print("\n拉取质量指标...")
    quality_map = fetch_quality_metrics(passing_symbols, use_cache=args.use_quality_cache)

    # ── 独立页面 ──────────────────────────────────────────────────────────────
    print(f"\n生成 {PROBE_PAGE.name} ...")
    probe_html = generate_probe_page(
        passed_30, universe, extra_map, profile_map, price_map, quality_map,
        sweep, cont2, cont3, data_ok, period_name, report_date, run_ts,
    )
    PROBE_PAGE.write_text(probe_html, encoding="utf-8")
    print(f"  {PROBE_PAGE.stat().st_size // 1024} KB")

    # ── stock-screener.html 摘要 ──────────────────────────────────────────────
    print(f"更新 {SCREENER_PAGE.name} 摘要节...")
    section = generate_screener_section(
        cnt_30, data_ok, sweep, cont2, cont3, period_name, report_date, run_ts,
    )
    content = SCREENER_PAGE.read_text(encoding="utf-8")
    if SECTION_START in content:
        s = content.index(SECTION_START)
        e = content.index(SECTION_END) + len(SECTION_END)
        content = content[:s] + content[e:]
        print("  (replaced existing section)")
    content = add_nav_link(content)
    inject_at = content.index(INJECT_BEFORE)
    content = content[:inject_at] + section + "\n" + content[inject_at:]
    SCREENER_PAGE.write_text(content, encoding="utf-8")
    print(f"  {SCREENER_PAGE.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
