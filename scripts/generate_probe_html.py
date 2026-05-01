#!/usr/bin/env python3
"""
读取 growth_gate_probe 缓存 JSONL，生成 HTML 报告段落并注入 stock-screener.html。

新增列：
  - 指数归属（沪深300 / 中证500 / 两者）
  - 扣非净利润同比增速（KCFJCXSYJLRTZ）
  - 增速分析标签（基于扣非/净利润对比 + 营收对比 + 多期趋势）

Usage:
  python3 scripts/generate_probe_html.py
"""
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import akshare as ak
import requests

BJT = timezone(timedelta(hours=8))
REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_JSONL = REPO_DIR / "artifacts" / "growth-gate-probe" / "fundamentals.jsonl"
DOCS_PAGE = Path("/home/ubuntu/docs-site/pages/stock-screener.html")

SECTION_START = "<!-- ===== GROWTH-GATE-PROBE ===== -->"
SECTION_END   = "<!-- ===== /GROWTH-GATE-PROBE ===== -->"
INJECT_BEFORE = "<section id=\"layer1\">"

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

# 扣非净利润同比、扣非净利润绝对值、毛利率、毛利率同比变化
EXTRA_COLS = ",".join([
    "SECURITY_CODE",
    "KCFJCXSYJLRTZ",   # 扣非净利润同比增速
    "KCFJCXSYJLR",     # 扣非净利润绝对值
    "XSMLL",           # 销售毛利率
    "XSMLL_TB",        # 毛利率同比变化 (pp)
    "OPERATE_PROFIT_PK",  # 营业利润
    "REPORT_DATE",
])


# ── 数据加载 ─────────────────────────────────────────────────────────────────

def load_universe() -> dict[str, dict]:
    """从 csindex.com.cn 拉取 CSI300+CSI500，返回 {symbol: {name, index}} 。"""
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


def fetch_extra_fields(symbols: list[str], batch_size: int = 50) -> dict[str, dict]:
    """
    对通过门槛的 N 只股票批量拉取扣非净利润 + 毛利率字段（最新1期）。
    返回 {symbol: {deduct_np_yoy, deduct_np, gross_margin, gross_margin_chg}} 。
    """
    result: dict[str, dict] = {s: {} for s in symbols}
    batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    print(f"  拉取 {len(symbols)} 只股票扣非净利润（{len(batches)} 批次）...")

    for i, batch in enumerate(batches, 1):
        codes_str = ",".join(f'"{s}"' for s in batch)
        try:
            r = requests.get(DC_URL, params={
                "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                "columns": EXTRA_COLS,
                "filter": f"(SECURITY_CODE in ({codes_str}))",
                "pageSize": len(batch),
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
            }, headers=DC_HEADERS, timeout=20)
            d = r.json()
            if d.get("success") and d.get("result"):
                seen: set[str] = set()
                for row in d["result"]["data"]:
                    code = row.get("SECURITY_CODE", "")
                    if code in result and code not in seen:
                        seen.add(code)
                        result[code] = {
                            "deduct_np_yoy":    row.get("KCFJCXSYJLRTZ"),
                            "deduct_np":        row.get("KCFJCXSYJLR"),
                            "gross_margin":     row.get("XSMLL"),
                            "gross_margin_chg": row.get("XSMLL_TB"),
                        }
        except Exception as e:
            print(f"  batch {i} error: {e}")
        time.sleep(0.3)

    ok = sum(1 for v in result.values() if v)
    print(f"  扣非数据: {ok}/{len(symbols)} 只拿到")
    return result


# ── 分析逻辑 ─────────────────────────────────────────────────────────────────

def passes_gate(periods: list[dict], thresh: float, cap: float = 200.0) -> bool:
    if not periods:
        return False
    p = periods[0]
    rg = p.get("revenue_growth")
    ng = p.get("net_profit_growth")
    return (rg is not None and ng is not None
            and rg >= thresh and ng >= thresh and ng <= cap)


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
    """
    生成增速分析标签。

    逻辑：
      1. 扣非 vs 净利润对比 → 判断非经常性损益贡献
      2. 扣非 vs 营收对比   → 判断利润率扩张 or 规模增长
      3. 多期趋势            → 业绩加速 flag
      4. 毛利率变化          → 毛利率方向
    """
    tags = []
    dng = extra.get("deduct_np_yoy")
    gm_chg = extra.get("gross_margin_chg")

    if dng is not None:
        gap = ng - dng  # 净利润增速 - 扣非增速，正值 = 非经常性收益拉高了净利润

        # ① 非经常性损益权重
        if gap > 50:
            tags.append("⚠ 非经常性收益主导")
        elif gap > 20:
            tags.append("非经常性收益显著")
        elif gap < -15:
            tags.append("非经常性损失拖累")

        # ② 扣非质量：扣非是正增长才有意义
        if dng >= 30:
            if rg > 0:
                ratio = dng / max(abs(rg), 1)
                if ratio > 1.4:
                    tags.append("利润率扩张")
                elif ratio < 0.6:
                    tags.append("规模增长为主")
                else:
                    tags.append("主业稳健增长")
            else:
                tags.append("主业强势")
        elif 0 <= dng < 30:
            tags.append("扣非增速较温和")
        else:
            tags.append("扣非承压")
    else:
        tags.append("扣非数据缺失")

    # ③ 多期加速
    if len(periods_data) >= 2:
        prev_ng = periods_data[1].get("net_profit_growth")
        prev_rg = periods_data[1].get("revenue_growth")
        if prev_ng is not None and ng - prev_ng > 30:
            tags.append("业绩加速↑")
        if prev_rg is not None and rg - prev_rg > 20:
            tags.append("营收提速")

    # ④ 毛利率方向
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


# ── HTML 生成 ─────────────────────────────────────────────────────────────────

def build_stock_row(r: dict, universe: dict, extra_map: dict, show_analysis: bool = True) -> str:
    p0 = r["periods"][0]
    sym = r["symbol"]
    rg  = p0["revenue_growth"]
    ng  = p0["net_profit_growth"]

    info  = universe.get(sym, {})
    name  = info.get("name", "")[:6]
    index = info.get("index", "—")
    index_color = {
        "沪深300": "#17becf",
        "中证500": "#3fb950",
        "两者":    "#ffd700",
    }.get(index, "var(--text-muted)")

    extra = extra_map.get(sym, {})
    dng   = extra.get("deduct_np_yoy")

    # 扣非净利润同比显示
    if dng is not None:
        dng_disp = f"{dng:+.1f}%"
        # 扣非增速与净利润增速差距
        gap = ng - dng
        if gap > 20:
            dng_color = "#e3b341"   # 橙色警告：非经常性收益显著
        elif dng >= 30:
            dng_color = "#3fb950"   # 绿色：扣非同样亮眼
        else:
            dng_color = "var(--text-muted)"
    else:
        dng_disp  = "—"
        dng_color = "var(--text-muted)"

    cont  = "✓" if passes_continuity(r["periods"], 15, 2) else "—"
    cont_color = "#3fb950" if cont == "✓" else "var(--text-muted)"

    analysis = classify_growth(rg, ng, extra, r["periods"]) if show_analysis else ""

    period_name = p0.get("period_name", "")

    return (
        f'<tr>'
        f'<td style="font-family:monospace;font-size:12px;">{sym}</td>'
        f'<td style="max-width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{name}</td>'
        f'<td style="text-align:center;"><span style="font-size:10px;color:{index_color};">{index}</span></td>'
        f'<td style="text-align:right;color:#17becf;">{rg:+.1f}%</td>'
        f'<td style="text-align:right;color:#9ecde6;">{ng:+.1f}%</td>'
        f'<td style="text-align:right;color:{dng_color};font-weight:600;">{dng_disp}</td>'
        f'<td style="text-align:center;color:{cont_color};font-size:12px;">{cont}</td>'
        f'<td style="font-size:11px;color:var(--text-muted);max-width:220px;">{analysis}</td>'
        f'<td style="font-size:11px;color:var(--text-muted);">{period_name}</td>'
        f'</tr>\n'
    )


def build_table_header() -> str:
    return """<thead>
      <tr>
        <th><span class="zh">代码</span><span class="en">Code</span></th>
        <th><span class="zh">名称</span><span class="en">Name</span></th>
        <th style="text-align:center;"><span class="zh">指数</span><span class="en">Index</span></th>
        <th style="text-align:right;"><span class="zh">营收同比</span><span class="en">Rev YoY</span></th>
        <th style="text-align:right;"><span class="zh">净利同比</span><span class="en">NP YoY</span></th>
        <th style="text-align:right;"><span class="zh">扣非净利同比</span><span class="en">Adj NP YoY</span></th>
        <th style="text-align:center;"><span class="zh">2期连续</span><span class="en">2-period</span></th>
        <th><span class="zh">增速分析</span><span class="en">Growth Analysis</span></th>
        <th><span class="zh">报告期</span><span class="en">Period</span></th>
      </tr>
    </thead>"""


def generate_html(records: list[dict], universe: dict, extra_map: dict, run_ts: str) -> str:
    # ── 统计 ─────────────────────────────────────────────────────────────────
    total = len(records)
    data_ok_list = [
        r for r in records
        if r["periods"] and
        r["periods"][0].get("revenue_growth") is not None and
        r["periods"][0].get("net_profit_growth") is not None
    ]
    data_ok    = len(data_ok_list)
    period_name = data_ok_list[0]["periods"][0]["period_name"] if data_ok_list else "—"
    report_date = data_ok_list[0]["periods"][0]["report_date"] if data_ok_list else "—"

    # 门槛扫描
    thresholds = [10, 15, 20, 25, 30, 35, 40, 50]
    sweep = []
    for th in thresholds:
        passed = [r for r in data_ok_list if passes_gate(r["periods"], th)]
        pct = len(passed) / data_ok * 100 if data_ok else 0
        target = (60 <= len(passed) <= 120)
        sweep.append((th, len(passed), pct, target))

    # 30%通过名单（按净利润同比降序）
    passed_30_raw = [r for r in data_ok_list if passes_gate(r["periods"], 30)]
    passed_30 = sorted(passed_30_raw, key=lambda x: -x["periods"][0]["net_profit_growth"])
    cnt_30 = len(passed_30)

    cont2 = [r for r in passed_30_raw if passes_continuity(r["periods"], 15, 2)]
    cont3 = [r for r in passed_30_raw if passes_continuity(r["periods"], 15, 3)]

    # 分布直方图
    buckets = [(-999, -50), (-50, 0), (0, 10), (10, 20), (20, 30),
               (30, 50), (50, 100), (100, 200), (200, 9999)]
    max_cnt = max(
        max(sum(1 for r in data_ok_list
                if lo <= r["periods"][0]["revenue_growth"] < hi) for lo, hi in buckets),
        1,
    )

    # ── 门槛扫描表 rows ───────────────────────────────────────────────────────
    sweep_rows = ""
    for th, cnt, pct, target in sweep:
        marker_zh = marker_en = hl = ""
        if target:
            marker_zh = '<span class="badge" style="background:#17becf22;color:#17becf;font-size:10px;margin-left:6px;">目标区间</span>'
            marker_en = '<span class="badge" style="background:#17becf22;color:#17becf;font-size:10px;margin-left:6px;">Target zone</span>'
            hl = ' style="background:#17becf0d;"'
        elif cnt < 30:
            marker_zh = '<span style="font-size:11px;color:var(--text-muted);margin-left:4px;">↑ 过稀</span>'
            marker_en = '<span style="font-size:11px;color:var(--text-muted);margin-left:4px;">↑ Too sparse</span>'
        elif cnt > 200:
            marker_zh = '<span style="font-size:11px;color:var(--text-muted);margin-left:4px;">↓ 过密</span>'
            marker_en = '<span style="font-size:11px;color:var(--text-muted);margin-left:4px;">↓ Too dense</span>'
        sweep_rows += (
            f'<tr{hl}>'
            f'<td>≥ {th}%</td>'
            f'<td style="text-align:right;font-variant-numeric:tabular-nums;">{cnt}</td>'
            f'<td style="text-align:right;font-variant-numeric:tabular-nums;">{pct:.1f}%</td>'
            f'<td><span class="zh">{marker_zh}</span><span class="en">{marker_en}</span></td>'
            f'</tr>\n'
        )

    # ── 股票表格 rows ─────────────────────────────────────────────────────────
    VISIBLE = 30
    stock_rows_visible = "".join(
        build_stock_row(r, universe, extra_map)
        for r in passed_30[:VISIBLE]
    )
    stock_rows_hidden = "".join(
        build_stock_row(r, universe, extra_map)
        for r in passed_30[VISIBLE:]
    )

    table_header = build_table_header()
    show_more_btn = ""
    if len(passed_30) > VISIBLE:
        extra = len(passed_30) - VISIBLE
        show_more_btn = f'''
  <details class="expandable" style="margin-top:8px;">
    <summary><span class="zh">展开剩余 {extra} 只</span><span class="en">Show {extra} more</span></summary>
    <div class="expandable-body" style="padding:0;overflow-x:auto;">
      <table style="font-size:12px;">
        {table_header}
        <tbody>{stock_rows_hidden}</tbody>
      </table>
    </div>
  </details>'''

    # ── 分布直方图 rows ───────────────────────────────────────────────────────
    def dist_rows(field: str) -> str:
        out = ""
        for lo, hi in buckets:
            cnt = sum(
                1 for r in data_ok_list
                if lo <= r["periods"][0][field] < hi
            )
            hi_s = "+∞" if hi == 9999 else f"{hi}%"
            label = f"[{lo}%, {hi_s})"
            flag_zh = "← 30%门槛" if lo == 30 else ""
            flag_en = "← 30% gate" if lo == 30 else ""
            color = "#17becf" if lo == 30 else ("#3fb950" if lo >= 0 else "#f85149")
            out += (
                f'<tr>'
                f'<td style="font-family:monospace;font-size:11px;white-space:nowrap;">{label}</td>'
                f'<td style="width:100%;">{bar(cnt, max_cnt // 8 + 1, color)}</td>'
                f'<td style="color:var(--text-muted);font-size:11px;white-space:nowrap;">'
                f'<span class="zh">{flag_zh}</span><span class="en">{flag_en}</span></td>'
                f'</tr>\n'
            )
        return out

    rev_rows = dist_rows("revenue_growth")
    np_rows  = dist_rows("net_profit_growth")

    pct_cont2 = len(cont2) / cnt_30 * 100 if cnt_30 else 0
    pct_cont3 = len(cont3) / cnt_30 * 100 if cnt_30 else 0

    # ── HTML 段落 ─────────────────────────────────────────────────────────────
    html = f'''
{SECTION_START}
<section id="growth-gate-probe" class="updated-latest">
  <h2>
    <span class="num" style="font-size:10px;">6.5</span>
    <span class="zh">成长门槛探针</span><span class="en">Growth Gate Probe</span>
    <span class="section-ts">{run_ts} BJT</span>
  </h2>

  <p>
    <span class="zh">对沪深300 + 中证500全量 800 只 A 股拉取近 3 期财报（东方财富 datacenter API，
    24h 可用），分析不同增速门槛下的候选池密度，验证 <strong>双 ≥30% 门槛</strong> 是否能将候选池控制在 60–120 只目标区间，
    并检验业绩连续性淘汰率。股票名单额外拉取 <strong>扣非净利润</strong> 字段，区分主业增长与非经常性损益的贡献。</span>
    <span class="en">Fetched latest 3 quarterly reports for all 800 CSI 300 + CSI 500 A-shares.
    Validates dual ≥30% gate, checks continuity attrition. The passing list adds
    <strong>adjusted net profit (excl. non-recurring items)</strong> to distinguish core growth from one-off items.</span>
  </p>

  <!-- stats cards -->
  <div class="stats-grid" style="margin-top:16px;">
    <div class="stat-card">
      <div class="value">{total}</div>
      <div class="label"><span class="zh">Universe</span><span class="en">Universe</span></div>
    </div>
    <div class="stat-card">
      <div class="value">{data_ok}</div>
      <div class="label"><span class="zh">双字段有效</span><span class="en">Both fields OK</span></div>
    </div>
    <div class="stat-card">
      <div class="value">{cnt_30}</div>
      <div class="label"><span class="zh">过双≥30%门槛</span><span class="en">Pass ≥30%/30%</span></div>
    </div>
    <div class="stat-card">
      <div class="value">{cnt_30/data_ok*100:.1f}%</div>
      <div class="label"><span class="zh">入选率</span><span class="en">Selection rate</span></div>
    </div>
  </div>

  <div class="callout" style="margin-top:16px;">
    <span class="zh">数据报告期：<strong>{period_name}</strong>（{report_date}）。筛选门槛用净利润同比（与主流分析一致）；通过名单额外补拉扣非净利润同比（<code>KCFJCXSYJLRTZ</code>），用于识别"净利润增速虚高"个股。</span>
    <span class="en">Data as of: <strong>{period_name}</strong> ({report_date}). Gate uses reported NP YoY (industry standard). Passing list also fetches adjusted NP YoY (<code>KCFJCXSYJLRTZ</code>) to flag stocks where headline profit is inflated by non-recurring items.</span>
  </div>

  <!-- threshold sweep -->
  <h3 style="margin-top:24px;"><span class="zh">门槛灵敏度（最新1期双门槛，净利同比上限200%）</span><span class="en">Threshold Sensitivity (latest quarter, dual gate, NP ≤ 200%)</span></h3>
  <table>
    <thead>
      <tr>
        <th><span class="zh">门槛</span><span class="en">Threshold</span></th>
        <th style="text-align:right;"><span class="zh">通过</span><span class="en">Pass</span></th>
        <th style="text-align:right;"><span class="zh">占比</span><span class="en">Pct</span></th>
        <th><span class="zh">备注</span><span class="en">Notes</span></th>
      </tr>
    </thead>
    <tbody>
{sweep_rows}    </tbody>
  </table>

  <!-- continuity -->
  <h3 style="margin-top:24px;"><span class="zh">连续性检验（最新期≥30% → 近N期均≥15%）</span><span class="en">Continuity Check (latest ≥30% → prior N periods ≥15%)</span></h3>
  <div class="card" style="padding:14px 18px;">
    <table style="margin:0;">
      <tr>
        <td><span class="zh">最新期 营收+净利 均≥30%</span><span class="en">Latest quarter both ≥30%</span></td>
        <td style="text-align:right;font-weight:600;">{cnt_30} 只</td>
        <td style="color:var(--text-muted);font-size:12px;">baseline</td>
      </tr>
      <tr>
        <td><span class="zh">+ 近2期均≥15%（连续两期）</span><span class="en">+ Prior 2 periods ≥15%</span></td>
        <td style="text-align:right;font-weight:600;">{len(cont2)} 只</td>
        <td style="color:var(--text-muted);font-size:12px;"><span class="zh">淘汰 {100-pct_cont2:.0f}%</span><span class="en">Attrition {100-pct_cont2:.0f}%</span></td>
      </tr>
      <tr>
        <td><span class="zh">+ 近3期均≥15%（连续三期）</span><span class="en">+ Prior 3 periods ≥15%</span></td>
        <td style="text-align:right;font-weight:600;">{len(cont3)} 只</td>
        <td style="color:var(--text-muted);font-size:12px;"><span class="zh">淘汰 {100-pct_cont3:.0f}%</span><span class="en">Attrition {100-pct_cont3:.0f}%</span></td>
      </tr>
    </table>
  </div>
  <p style="font-size:12px;color:var(--text-muted);margin-top:6px;">
    <span class="zh">MVP 设计：最新期≥30% 作为硬门槛；连续性（近2-3期≥15%）在 Dislocation 通道作为加权排序因子。</span>
    <span class="en">MVP design: latest ≥30% is a hard gate; continuity used as weighting in Dislocation channel.</span>
  </p>

  <!-- passing list -->
  <h3 style="margin-top:24px;">
    <span class="zh">双≥30%通过名单（{cnt_30} 只，按净利同比降序）</span>
    <span class="en">Dual ≥30% Passing List ({cnt_30} stocks, sorted by NP YoY desc)</span>
  </h3>
  <div class="callout" style="margin-bottom:10px;padding:10px 14px;">
    <span class="zh">
      <strong>列说明</strong>：
      净利同比 = 含非经常性损益的归母净利润增速（筛选门槛依据）；
      <strong>扣非净利同比</strong> = 扣除非经常性损益后的归母净利润增速（反映主业真实增长）；
      两列差距大时（标橙）说明净利润有一次性收益撑高。
      指数：<span style="color:#17becf;">■</span> 沪深300&nbsp;
      <span style="color:#3fb950;">■</span> 中证500&nbsp;
      <span style="color:#ffd700;">■</span> 两者均有。
    </span>
    <span class="en">
      <strong>Column notes</strong>:
      NP YoY = reported NP incl. non-recurring items (gate basis);
      <strong>Adj NP YoY</strong> = net profit excl. non-recurring items (reflects true core growth).
      Large gap (orange) = headline NP inflated by one-off gains.
      Index: <span style="color:#17becf;">■</span> CSI300&nbsp;
      <span style="color:#3fb950;">■</span> CSI500&nbsp;
      <span style="color:#ffd700;">■</span> Both.
    </span>
  </div>
  <div style="overflow-x:auto;">
  <table style="font-size:12px;">
    {table_header}
    <tbody>
{stock_rows_visible}    </tbody>
  </table>
  </div>
{show_more_btn}

  <!-- distribution charts -->
  <details class="expandable" style="margin-top:24px;">
    <summary><span class="zh">增速分布直方图（营收同比 &amp; 净利润同比）</span><span class="en">Growth Rate Distribution (Revenue YoY &amp; Net Profit YoY)</span></summary>
    <div class="expandable-body">
      <style>
        .dist-bar-wrap {{ display:flex; align-items:center; gap:6px; width:100%; }}
        .dist-bar {{ height:14px; border-radius:3px; min-width:2px; transition:width .3s; }}
        .dist-cnt {{ font-size:11px; color:var(--text-muted); white-space:nowrap; }}
      </style>

      <h3><span class="zh">营收同比分布（{data_ok} 只）</span><span class="en">Revenue YoY Distribution ({data_ok} stocks)</span></h3>
      <table style="font-size:12px;width:100%;"><tbody>{rev_rows}</tbody></table>

      <h3 style="margin-top:16px;"><span class="zh">净利润同比分布（{data_ok} 只）</span><span class="en">Net Profit YoY Distribution ({data_ok} stocks)</span></h3>
      <table style="font-size:12px;width:100%;"><tbody>{np_rows}</tbody></table>
    </div>
  </details>

  <p style="font-size:12px;color:var(--text-muted);margin-top:12px;">
    <span class="zh">脚本: <code>scripts/growth_gate_probe.py</code> + <code>scripts/generate_probe_html.py</code> | 扣非字段: <code>KCFJCXSYJLRTZ</code> | 数据源: datacenter-web.eastmoney.com</span>
    <span class="en">Scripts: <code>growth_gate_probe.py</code> + <code>generate_probe_html.py</code> | Adj NP field: <code>KCFJCXSYJLRTZ</code> | Source: datacenter-web.eastmoney.com</span>
  </p>
</section>
{SECTION_END}
'''
    return html


# ── 页面注入 ──────────────────────────────────────────────────────────────────

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
    run_ts = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")

    print("Fetching universe (names + index membership) from csindex.com.cn...")
    universe = load_universe()
    print(f"  {len(universe)} stocks loaded")

    print(f"Loading fundamentals cache: {CACHE_JSONL}")
    records = load_cache()
    print(f"  {len(records)} records loaded")

    # 先算出通过30%门槛的股票，只为它们拉扣非数据
    data_ok_list = [
        r for r in records
        if r["periods"] and
        r["periods"][0].get("revenue_growth") is not None and
        r["periods"][0].get("net_profit_growth") is not None
    ]
    passing_symbols = [
        r["symbol"] for r in data_ok_list
        if passes_gate(r["periods"], 30)
    ]
    print(f"\n过30%门槛: {len(passing_symbols)} 只，开始拉扣非净利润...")
    extra_map = fetch_extra_fields(passing_symbols)

    print("\nGenerating HTML section...")
    section_html = generate_html(records, universe, extra_map, run_ts)

    print(f"Injecting into {DOCS_PAGE}...")
    content = DOCS_PAGE.read_text(encoding="utf-8")

    if SECTION_START in content:
        start_idx = content.index(SECTION_START)
        end_idx = content.index(SECTION_END) + len(SECTION_END)
        content = content[:start_idx] + content[end_idx:]
        print("  (replaced existing section)")

    content = add_nav_link(content)

    inject_at = content.index(INJECT_BEFORE)
    content = content[:inject_at] + section_html + "\n" + content[inject_at:]

    DOCS_PAGE.write_text(content, encoding="utf-8")
    print(f"  Done — {DOCS_PAGE.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
