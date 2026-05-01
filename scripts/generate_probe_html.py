#!/usr/bin/env python3
"""
读取 growth_gate_probe 缓存 JSONL，生成 HTML 报告段落并注入 stock-screener.html。

Usage:
  python3 scripts/generate_probe_html.py
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJT = timezone(timedelta(hours=8))
REPO_DIR = Path(__file__).resolve().parent.parent
CACHE_JSONL = REPO_DIR / "artifacts" / "growth-gate-probe" / "fundamentals.jsonl"
DOCS_PAGE = Path("/home/ubuntu/docs-site/pages/stock-screener.html")

SECTION_START = "<!-- ===== GROWTH-GATE-PROBE ===== -->"
SECTION_END   = "<!-- ===== /GROWTH-GATE-PROBE ===== -->"
INJECT_BEFORE = "<section id=\"layer1\">"


def load_cache() -> list[dict]:
    records = []
    for line in CACHE_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


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


def bar(count: int, scale: int, color: str = "#17becf") -> str:
    width = min(100, max(0, count * 100 // max(scale, 1)))
    return (f'<div class="dist-bar-wrap">'
            f'<div class="dist-bar" style="width:{width}%;background:{color}"></div>'
            f'<span class="dist-cnt">{count}</span></div>')


def generate_html(records: list[dict], run_ts: str) -> str:
    # ── stats ────────────────────────────────────────────────────────────────
    total = len(records)
    data_ok_list = [
        r for r in records
        if r["periods"] and
        r["periods"][0].get("revenue_growth") is not None and
        r["periods"][0].get("net_profit_growth") is not None
    ]
    data_ok = len(data_ok_list)
    period_name = data_ok_list[0]["periods"][0]["period_name"] if data_ok_list else "—"
    report_date = data_ok_list[0]["periods"][0]["report_date"] if data_ok_list else "—"

    # threshold sweep
    thresholds = [10, 15, 20, 25, 30, 35, 40, 50]
    sweep = []
    for th in thresholds:
        passed = [r for r in data_ok_list if passes_gate(r["periods"], th)]
        pct = len(passed) / data_ok * 100 if data_ok else 0
        target = (60 <= len(passed) <= 120)
        sweep.append((th, len(passed), pct, target))

    # passing 30% list
    passed_30_raw = [r for r in data_ok_list if passes_gate(r["periods"], 30)]
    passed_30 = sorted(passed_30_raw, key=lambda x: -x["periods"][0]["net_profit_growth"])
    cnt_30 = len(passed_30)

    # continuity (periods ≥ 2)
    cont2 = [r for r in passed_30_raw if passes_continuity(r["periods"], 15, 2)]
    cont3 = [r for r in passed_30_raw if passes_continuity(r["periods"], 15, 3)]

    # distribution buckets
    buckets = [(-999, -50), (-50, 0), (0, 10), (10, 20), (20, 30),
               (30, 50), (50, 100), (100, 200), (200, 9999)]
    max_cnt = max(
        max(sum(1 for r in data_ok_list
                if lo <= r["periods"][0]["revenue_growth"] < hi) for lo, hi in buckets),
        1,
    )

    # ── HTML ─────────────────────────────────────────────────────────────────
    # threshold rows
    sweep_rows = ""
    for th, cnt, pct, target in sweep:
        marker_zh = ""
        marker_en = ""
        hl = ""
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

    # stock table rows (top 30 visible, rest collapsed)
    stock_rows_visible = ""
    stock_rows_hidden = ""
    for i, r in enumerate(passed_30):
        p0 = r["periods"][0]
        rg = p0["revenue_growth"]
        ng = p0["net_profit_growth"]
        cont = "✓" if passes_continuity(r["periods"], 15, 2) else "—"
        cont_color = "#3fb950" if cont == "✓" else "var(--text-muted)"
        row = (
            f'<tr>'
            f'<td style="font-family:monospace;font-size:12px;">{r["symbol"]}</td>'
            f'<td>{r.get("name","")[:6]}</td>'
            f'<td style="text-align:right;color:#17becf;">{rg:+.1f}%</td>'
            f'<td style="text-align:right;color:#3fb950;">{ng:+.1f}%</td>'
            f'<td style="text-align:center;color:{cont_color};font-size:12px;">{cont}</td>'
            f'<td style="font-size:11px;color:var(--text-muted);">{p0.get("period_name","")}</td>'
            f'</tr>\n'
        )
        if i < 30:
            stock_rows_visible += row
        else:
            stock_rows_hidden += row

    show_more_btn = ""
    if len(passed_30) > 30:
        extra = len(passed_30) - 30
        show_more_btn = f'''
  <details class="expandable" style="margin-top:8px;">
    <summary><span class="zh">展开剩余 {extra} 只</span><span class="en">Show {extra} more</span></summary>
    <div class="expandable-body" style="padding:0;">
      <table style="font-size:12px;">
        <thead><tr>
          <th><span class="zh">代码</span><span class="en">Code</span></th>
          <th><span class="zh">名称</span><span class="en">Name</span></th>
          <th><span class="zh">营收同比</span><span class="en">Rev YoY</span></th>
          <th><span class="zh">净利同比</span><span class="en">NP YoY</span></th>
          <th><span class="zh">2期连续</span><span class="en">2-period</span></th>
          <th><span class="zh">报告期</span><span class="en">Period</span></th>
        </tr></thead>
        <tbody>{stock_rows_hidden}</tbody>
      </table>
    </div>
  </details>'''

    # distribution bars
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

    # continuity numbers
    pct_cont2 = len(cont2) / cnt_30 * 100 if cnt_30 else 0
    pct_cont3 = len(cont3) / cnt_30 * 100 if cnt_30 else 0

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
    24h 可用），分析不同增速门槛下的候选池密度，验证 <strong>双 ≥30% 门槛</strong> 是否能将候选池
    控制在 60–120 只目标区间，并检验业绩连续性淘汰率。</span>
    <span class="en">Fetched the latest 3 quarterly reports for all 800 CSI 300 + CSI 500 A-shares
    (East Money datacenter API, 24h available). Measures candidate pool density at various
    growth rate thresholds to validate whether the <strong>dual ≥30% gate</strong> holds the pool
    within the 60–120 target range, and checks continuity attrition rates.</span>
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
    <span class="zh">数据报告期：<strong>{period_name}</strong>（{report_date}），采用 datacenter-web.eastmoney.com
    批量拉取（每批 50 只，16 批次）。运行时间约 1 分钟（缓存命中）。</span>
    <span class="en">Data as of: <strong>{period_name}</strong> ({report_date}). Fetched via
    datacenter-web.eastmoney.com in batches of 50 (16 batches total). Runtime ~1 min (cache hit).</span>
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
    <span class="zh">MVP 设计：最新期≥30% 作为硬门槛；连续性（近2-3期≥15%）在 Dislocation 通道升级为加权排序因子，不作二次硬截断。</span>
    <span class="en">MVP design: latest ≥30% is a hard gate; continuity (prior 2–3 periods ≥15%) will be used as a weighting factor in the Dislocation channel, not a second hard cutoff.</span>
  </p>

  <!-- passing list -->
  <h3 style="margin-top:24px;"><span class="zh">双≥30%通过名单（{cnt_30} 只，按净利同比降序）</span><span class="en">Dual ≥30% Passing List ({cnt_30} stocks, sorted by NP YoY desc)</span></h3>
  <p style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">
    <span class="zh">2期连续 ✓ = 近2期营收+净利同比均≥15%。</span>
    <span class="en">2-period ✓ = prior 2 periods both rev &amp; NP YoY ≥15%.</span>
  </p>
  <table style="font-size:12px;">
    <thead>
      <tr>
        <th><span class="zh">代码</span><span class="en">Code</span></th>
        <th><span class="zh">名称</span><span class="en">Name</span></th>
        <th style="text-align:right;"><span class="zh">营收同比</span><span class="en">Rev YoY</span></th>
        <th style="text-align:right;"><span class="zh">净利同比</span><span class="en">NP YoY</span></th>
        <th style="text-align:center;"><span class="zh">2期连续</span><span class="en">2-period</span></th>
        <th><span class="zh">报告期</span><span class="en">Period</span></th>
      </tr>
    </thead>
    <tbody>
{stock_rows_visible}    </tbody>
  </table>
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
      <table style="font-size:12px;width:100%;">
        <tbody>{rev_rows}</tbody>
      </table>

      <h3 style="margin-top:16px;"><span class="zh">净利润同比分布（{data_ok} 只）</span><span class="en">Net Profit YoY Distribution ({data_ok} stocks)</span></h3>
      <table style="font-size:12px;width:100%;">
        <tbody>{np_rows}</tbody>
      </table>
    </div>
  </details>

  <p style="font-size:12px;color:var(--text-muted);margin-top:12px;">
    <span class="zh">脚本: <code>scripts/growth_gate_probe.py</code> | 缓存: <code>artifacts/growth-gate-probe/fundamentals.jsonl</code> | 数据源: datacenter-web.eastmoney.com</span>
    <span class="en">Script: <code>scripts/growth_gate_probe.py</code> | Cache: <code>artifacts/growth-gate-probe/fundamentals.jsonl</code> | Source: datacenter-web.eastmoney.com</span>
  </p>
</section>
{SECTION_END}
'''
    return html


def inject_into_page(section_html: str) -> None:
    content = DOCS_PAGE.read_text(encoding="utf-8")

    # Remove existing section if present
    if SECTION_START in content:
        start_idx = content.index(SECTION_START)
        end_idx = content.index(SECTION_END) + len(SECTION_END)
        content = content[:start_idx] + content[end_idx:]
        print("  (replaced existing growth-gate-probe section)")

    # Insert before layer1 section
    inject_at = content.index(INJECT_BEFORE)
    content = content[:inject_at] + section_html + "\n" + content[inject_at:]

    DOCS_PAGE.write_text(content, encoding="utf-8")
    print(f"  Wrote {DOCS_PAGE}")


def add_nav_link(html: str) -> str:
    """Add nav link for growth-gate-probe if not present."""
    marker = 'href="#growth-gate-probe"'
    if marker in html:
        return html  # already there

    # Insert after the phase0-results nav link
    after = 'href="#phase0-results"'
    idx = html.index(after)
    # find end of that line
    line_end = html.index("\n", idx) + 1
    new_link = ('    <a href="#growth-gate-probe" style="padding-left:32px;font-size:12px;">'
                '<span class="zh">↳ 成长门槛探针</span>'
                '<span class="en">↳ Growth Gate Probe</span>'
                ' <span class="badge" style="background:#ffd70033;color:#ffd700;">NEW</span></a>\n')
    return html[:line_end] + new_link + html[line_end:]


def main() -> None:
    run_ts = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")

    print(f"Loading cache: {CACHE_JSONL}")
    records = load_cache()
    print(f"  {len(records)} records loaded")

    print("Generating HTML section...")
    section_html = generate_html(records, run_ts)

    print(f"Injecting into {DOCS_PAGE}...")
    content = DOCS_PAGE.read_text(encoding="utf-8")

    # Remove existing section if present
    if SECTION_START in content:
        start_idx = content.index(SECTION_START)
        end_idx = content.index(SECTION_END) + len(SECTION_END)
        content = content[:start_idx] + content[end_idx:]
        print("  (replaced existing section)")

    # Add nav link
    content = add_nav_link(content)

    # Insert before layer1 section
    inject_at = content.index(INJECT_BEFORE)
    content = content[:inject_at] + section_html + "\n" + content[inject_at:]

    DOCS_PAGE.write_text(content, encoding="utf-8")
    print(f"  Done — {DOCS_PAGE.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
