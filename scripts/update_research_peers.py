#!/usr/bin/env python3
"""
update_research_peers.py — 全球竞争格局同行数据更新

数据源：
  - 纯光学 A 股同行（光迅科技/新易盛/天孚通信）：akshare stock_financial_abstract
  - 海外上市公司（COHR/AAOI/LITE）：yfinance 年报财务
    * COHR Networking 分部值人工维护，每年从 10-K 更新
  - 多元化同行分部数据（华工科技/东山精密）：
    * 默认（月度 cron）：跳过 PDF 提取，只记录占位
    * --with-pdf：从巨潮年报 PDF + Claude Haiku 提取光通信分部营收
  - 旭创自身（300308）：读取已有 300308-financials.json
  - USD/CNY 汇率：yfinance USDCNY=X

输出：docs-site/data/300308-peers.json → /var/www/overview/data/300308-peers.json

Cron（月度，不含 PDF）：
  30 0 1 * *  ~/cron-wrapper.sh --name research-peers --timeout 120 ...
季度人工（含 PDF 分部提取）：
  python3 update_research_peers.py --with-pdf
"""

import argparse
import json
import math
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time
import warnings
from datetime import datetime, timedelta, timezone

import akshare as ak
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

# ── paths ──────────────────────────────────────────────────────────────────────
REPO_DIR = pathlib.Path(__file__).resolve().parents[1]
DOCS_SITE_DIR = pathlib.Path(os.path.expanduser("~/docs-site"))
DATA_DIR = DOCS_SITE_DIR / "data"
DEPLOY_DATA_DIR = pathlib.Path("/var/www/overview/data")
BJT = timezone(timedelta(hours=8))

# ── Anthropic（仅 --with-pdf 时使用）────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    _oc_env = pathlib.Path.home() / ".openclaw" / ".env"
    if _oc_env.exists():
        for _line in _oc_env.read_text().splitlines():
            if _line.startswith("ANTHROPIC_API_KEY="):
                ANTHROPIC_API_KEY = _line.split("=", 1)[1].strip()
                break

_claude = None


def _get_claude():
    global _claude
    if _claude is None:
        import anthropic
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _claude


# ── helpers ────────────────────────────────────────────────────────────────────
def _safe(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _round1(v) -> float | None:
    return round(v, 1) if v is not None else None


# ── exchange rate ──────────────────────────────────────────────────────────────
def fetch_usd_cny() -> float:
    t = yf.Ticker("USDCNY=X")
    rate = _safe(t.fast_info.last_price)
    if rate is None or rate < 5 or rate > 12:
        raise ValueError(f"Unexpected USDCNY rate: {rate}")
    return round(rate, 4)


# ── configs ────────────────────────────────────────────────────────────────────
# 海外上市公司：yfinance 拉总量；COHR 额外有 manual_segment 覆盖 Networking 分部
_OVERSEAS_CONFIGS = [
    {
        "key": "cohr",
        "ticker": "COHR",
        "name": "Coherent Corp",
        "country": "US",
        "lc_rank": "#2",
        "position": "长距 + 短距全覆盖，InP 激光器垂直整合",
        # Networking 分部（光通信）值人工维护，每年从 COHR 10-K 更新
        # Source: FY2025 10-K (year ended June 30, 2025) — Networking segment
        "manual_segment_usd_b": 3.0,
        "manual_segment_label": "Networking 分部（光通信）",
        "manual_segment_source": "FY2025 10-K，人工维护",
    },
    {
        "key": "aaoi",
        "ticker": "AAOI",
        "name": "Applied Optoelectronics (AAOI)",
        "country": "US",
        "lc_rank": "~#7",
        "position": "超大规模数据中心专注，低成本竞争策略",
    },
    {
        "key": "lite",
        "ticker": "LITE",
        "name": "Lumentum (LITE)",
        "country": "US",
        "lc_rank": "上游",
        "position": "EML 激光器核心供应商（50–60% 全球份额）",
    },
]

# 纯光学 A 股同行：akshare 总营收即光学业务，直接可用
_DOMESTIC_CONFIGS = [
    {
        "key": "eoptolink",
        "symbol": "300502",
        "name": "新易盛 Eoptolink",
        "country": "CN",
        "lc_rank": "#3（由 #7 升）",
        "position": "数据中心专注，硅光先行，国内毛利率最高",
    },
    {
        "key": "tianfu",
        "symbol": "300394",
        "name": "天孚通信",
        "country": "CN",
        "lc_rank": "器件",
        "position": "无源光器件龙头（FA/MT 连接器全球 #1）",
    },
    {
        "key": "optoway",
        "symbol": "002281",
        "name": "光迅科技 Optoway",
        "country": "CN",
        "lc_rank": "~#4",
        "position": "光模块 + 器件，5G + 数据中心双线，国有背景（武汉邮科）",
    },
]

# 多元化同行：需从年报 PDF 提取光模块/光通信分部营收（--with-pdf 时运行）
_PDF_SEGMENT_CONFIGS = [
    {
        "key": "huagong",
        "symbol": "000988",
        "name": "华工科技",
        "country": "CN",
        "lc_rank": "器件",
        "position": "华工正源光子：光器件 + 光模块，5G + IDC",
        "segment_label": "光通信业务分部（华工正源光子）",
        "optical_keywords": ["光通信", "光器件", "华工正源", "光模块", "光子器件"],
    },
    {
        "key": "dongshan",
        "symbol": "002384",
        "name": "东山精密",
        "country": "CN",
        "lc_rank": "制造",
        "position": "精密制造跨界，光模块新兴增长极",
        "segment_label": "光模块业务分部",
        "optical_keywords": ["光模块", "光通信", "光学", "光器件", "光学器件"],
    },
]


# ── overseas peer fetch ────────────────────────────────────────────────────────
def fetch_overseas_peer(cfg: dict) -> dict:
    ticker = cfg["ticker"]
    t = yf.Ticker(ticker)
    fin = t.financials

    if fin is None or fin.empty:
        raise ValueError(f"[{ticker}] yfinance returned empty financials")

    col = fin.columns[0]
    fiscal_label = f"FY{col.year}"

    rev = _safe(fin.loc["Total Revenue", col]) if "Total Revenue" in fin.index else None
    net = _safe(fin.loc["Net Income", col]) if "Net Income" in fin.index else None
    gp = _safe(fin.loc["Gross Profit", col]) if "Gross Profit" in fin.index else None

    if rev is None:
        raise ValueError(f"[{ticker}] Total Revenue not found")

    net_margin = _round1(net / rev * 100) if net is not None else None
    gross_margin = _round1(gp / rev * 100) if gp is not None else None

    p: dict = {
        "key": cfg["key"],
        "name": cfg["name"],
        "ticker": ticker,
        "country": "US",
        "fiscal_year": fiscal_label,
        "revenue_usd_b": round(rev / 1e9, 2),
        "net_margin_pct": net_margin,
        "gross_margin_pct": gross_margin,
        "lc_rank": cfg.get("lc_rank", ""),
        "position": cfg.get("position", ""),
    }

    # 覆盖为分部数据（如 COHR Networking segment 人工维护值）
    if cfg.get("manual_segment_usd_b"):
        p["revenue_usd_b"] = cfg["manual_segment_usd_b"]
        p["revenue_total_usd_b"] = round(rev / 1e9, 2)  # 保存总量供参考
        p["revenue_is_segment"] = True
        p["segment_label"] = cfg["manual_segment_label"]
        p["segment_source"] = cfg["manual_segment_source"]

    return p


# ── domestic pure-play peer fetch ──────────────────────────────────────────────
def fetch_domestic_peer(cfg: dict, usd_cny: float) -> dict:
    symbol = cfg["symbol"]
    df = ak.stock_financial_abstract(symbol=symbol)

    annual_cols = sorted(
        [c for c in df.columns if c.isdigit() and len(c) == 8 and c.endswith("1231")],
        reverse=True,
    )
    if not annual_cols:
        raise ValueError(f"[{symbol}] No annual columns found")

    col = annual_cols[0]
    fiscal_label = col[:4] + "A"

    def get(indicator: str, selection: str) -> float | None:
        rows = df[(df["指标"] == indicator) & (df["选项"] == selection)]
        return _safe(rows.iloc[0][col]) if not rows.empty else None

    rev = get("营业总收入", "常用指标")
    net_margin = _round1(get("销售净利率", "常用指标"))
    gross_margin = _round1(get("毛利率", "常用指标"))

    if rev is None:
        raise ValueError(f"[{symbol}] Revenue not found for {col}")

    return {
        "key": cfg["key"],
        "name": cfg["name"],
        "ticker": f"{symbol}.SZ",
        "country": "CN",
        "fiscal_year": fiscal_label,
        "revenue_cny_yi": round(rev / 1e8),
        "revenue_usd_b": round(rev / (usd_cny * 1e9), 2),
        "net_margin_pct": net_margin,
        "gross_margin_pct": gross_margin,
        "lc_rank": cfg.get("lc_rank", ""),
        "position": cfg.get("position", ""),
    }


# ── innolight self-reference ───────────────────────────────────────────────────
def load_innolight_peer(usd_cny: float) -> dict:
    src = DATA_DIR / "300308-financials.json"
    if not src.exists():
        raise FileNotFoundError(f"300308-financials.json not found: {src}")

    d = json.loads(src.read_text(encoding="utf-8"))
    la = d.get("latest_annual", {})

    rev = la.get("revenue_yi")
    if rev is None:
        raise ValueError("旭创 revenue_yi not found in 300308-financials.json")

    return {
        "key": "innolight",
        "name": "旭创 InnoLight",
        "ticker": "300308.SZ",
        "country": "CN",
        "fiscal_year": la.get("year", ""),
        "revenue_cny_yi": int(rev),
        "revenue_usd_b": round(rev * 1e8 / (usd_cny * 1e9), 2),
        "net_margin_pct": la.get("net_margin_pct"),
        "gross_margin_pct": la.get("gross_margin_pct"),
        "lc_rank": "#1",
        "position": "数据中心绝对领先，800G 主力，1.6T 先发",
    }


# ── PDF segment extraction helpers ────────────────────────────────────────────
_CNINFO_PDF_BASE = "https://static.cninfo.com.cn/finalpage/"
_ANNUAL_FULL_PATTERN = re.compile(r"^\d{4}年年度报告$")
_EXCLUDE_PATTERN = re.compile(r"摘要|半年度|关于|更正|补充|英文|取消|提示|差错")

# 分部/产品营收关键词（取前 2 页 per 类别）
_SEG_PAGE_CATS: dict[str, list[str]] = {
    "by_product": ["主营业务收入", "分产品", "按产品分类", "产品类别", "主要业务收入分类"],
    "business_seg": ["分部信息", "业务板块", "经营分部", "报告分部", "分部收入"],
    "optical_kw": [],  # 动态注入 per-company 关键词
}


def _find_annual_report_url(symbol: str) -> tuple[str, str, str] | None:
    start = (datetime.now(BJT) - timedelta(days=365 * 2)).strftime("%Y%m%d")
    end = datetime.now(BJT).strftime("%Y%m%d")
    df = ak.stock_zh_a_disclosure_report_cninfo(
        symbol=symbol, market="沪深京", keyword="年度报告",
        start_date=start, end_date=end,
    )
    if df.empty:
        return None
    for _, row in df.iterrows():
        raw_title = row.get("公告标题", "")
        clean_title = re.sub(r"<[^>]+>", "", raw_title)
        ann_time = str(row.get("公告时间", ""))[:10]
        if not _ANNUAL_FULL_PATTERN.match(clean_title):
            continue
        if _EXCLUDE_PATTERN.search(clean_title):
            continue
        link = row.get("公告链接", "")
        m = re.search(r"announcementId=(\d+)", link)
        if not m:
            continue
        pdf_url = f"{_CNINFO_PDF_BASE}{ann_time}/{m.group(1)}.PDF"
        return pdf_url, clean_title, ann_time
    return None


def _download_pdf(url: str, dest: pathlib.Path) -> None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.cninfo.com.cn/",
    }
    r = requests.get(url, headers=headers, timeout=120, stream=True)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)


def _extract_segment_pages(pdf_path: pathlib.Path, optical_keywords: list[str]) -> tuple[str, int]:
    """提取分部/分产品收入相关页面，最多 8 页。"""
    import pdfplumber
    all_page_texts: list[tuple[int, str]] = []
    total_pages = 0
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if text.strip():
                all_page_texts.append((i + 1, text))

    # 动态注入公司光学关键词
    cats = {
        "by_product": _SEG_PAGE_CATS["by_product"],
        "business_seg": _SEG_PAGE_CATS["business_seg"],
        "optical_kw": optical_keywords,
    }

    selected: set[int] = set()
    for kws in cats.values():
        matches = [(idx, t) for idx, t in all_page_texts if any(kw in t for kw in kws)]
        for idx, _ in matches[:2]:
            selected.add(idx)

    pages = sorted([(idx, t) for idx, t in all_page_texts if idx in selected], key=lambda x: x[0])[:8]
    text = "\n\n".join(f"--- 第{idx}页 ---\n{t}" for idx, t in pages)
    return text, total_pages


def _extract_segment_with_llm(company_name: str, pdf_text: str) -> dict | None:
    """调用 Claude Haiku 提取光模块/光通信分部营收。"""
    client = _get_claude()
    prompt = (
        "以下是" + company_name + "年度报告部分文字内容。\n"
        "请从中提取光模块/光通信/光器件相关业务的分部营收数据。\n\n"
        "以 JSON 格式输出（只输出合法 JSON，不加任何注释或说明文字）：\n"
        '{"optical_segment": {"name": "分部名称", "revenue_yi": X, '
        '"fiscal_year": "2025A", "pct_of_total": X_或_null, "note": "简短说明"}}\n\n'
        "若文中无法找到光模块/光通信相关分部营收数据，返回：\n"
        '{"optical_segment": null}\n\n'
        "年度报告文字内容：\n"
    ) + pdf_text[:12000]

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise ValueError(f"LLM 返回无法解析的内容: {raw[:200]}")
    result = json.loads(m.group())
    return result.get("optical_segment")


def fetch_segment_peer(cfg: dict, usd_cny: float) -> dict:
    """
    下载年报 PDF → 提取光通信分部营收。
    同时拉取 akshare 总量财务数据用于对比展示。
    """
    symbol = cfg["symbol"]
    company_name = cfg["name"]
    optical_keywords = cfg.get("optical_keywords", [])

    # Step 1: akshare 总量财务（用于填充 revenue_total 和 margins）
    df = ak.stock_financial_abstract(symbol=symbol)
    annual_cols = sorted(
        [c for c in df.columns if c.isdigit() and len(c) == 8 and c.endswith("1231")],
        reverse=True,
    )
    col = annual_cols[0] if annual_cols else None
    fiscal_label = (col[:4] + "A") if col else "—"

    def get(ind: str, sel: str) -> float | None:
        if col is None:
            return None
        rows = df[(df["指标"] == ind) & (df["选项"] == sel)]
        return _safe(rows.iloc[0][col]) if not rows.empty else None

    total_rev = get("营业总收入", "常用指标")
    net_margin = _round1(get("销售净利率", "常用指标"))
    gross_margin = _round1(get("毛利率", "常用指标"))
    total_cny_yi = round(total_rev / 1e8) if total_rev else None

    time.sleep(0.5)

    # Step 2: 找年报 PDF
    print(f"    [{symbol}] 查询年报 PDF...", flush=True)
    pdf_info = _find_annual_report_url(symbol)
    if pdf_info is None:
        raise ValueError(f"[{symbol}] cninfo 未找到完整年报 PDF")
    pdf_url, pdf_title, pdf_date = pdf_info
    print(f"    [{symbol}] 找到: {pdf_title}", flush=True)

    # Step 3: 下载 + 提取分部页面
    segment = None
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = pathlib.Path(tmpdir) / f"{symbol}-annual.pdf"
        print(f"    [{symbol}] 下载 PDF...", flush=True)
        _download_pdf(pdf_url, pdf_path)
        size_kb = pdf_path.stat().st_size // 1024
        print(f"    [{symbol}] 下载完成 ({size_kb} KB)", flush=True)

        seg_text, total_pages = _extract_segment_pages(pdf_path, optical_keywords)
        print(f"    [{symbol}] 共 {total_pages} 页，提取到 {len(seg_text)} 字符", flush=True)

        if seg_text.strip():
            print(f"    [{symbol}] 调用 Claude Haiku 提取分部营收...", flush=True)
            segment = _extract_segment_with_llm(company_name, seg_text)
            print(f"    [{symbol}] 分部提取结果: {segment}", flush=True)

    # Step 4: 组装结果
    p: dict = {
        "key": cfg["key"],
        "name": company_name,
        "ticker": f"{symbol}.SZ",
        "country": "CN",
        "fiscal_year": fiscal_label,
        "revenue_cny_yi": total_cny_yi,
        "revenue_usd_b": round(total_rev / (usd_cny * 1e9), 2) if total_rev else None,
        "net_margin_pct": net_margin,
        "gross_margin_pct": gross_margin,
        "lc_rank": cfg.get("lc_rank", ""),
        "position": cfg.get("position", ""),
        "revenue_is_segment": False,
    }

    if segment:
        seg_yi = _safe(segment.get("revenue_yi"))
        total_yi = (total_rev / 1e8) if total_rev else None

        # Sanity check 双层校验（LLM 常把 元 ÷ 1e7 而非 ÷ 1e8）
        pct = _safe(segment.get("pct_of_total"))
        pct_based_yi = round(total_yi * pct / 100, 1) if (pct and total_yi and 0 < pct < 100) else None

        if seg_yi and total_yi:
            # 层1：分部值超过总营收
            exceeds_total = seg_yi > total_yi * 1.05
            # 层2：与 pct 推算值差距 >3x（LLM 单位换算错误的典型特征）
            pct_mismatch = (
                pct_based_yi is not None and pct_based_yi > 0
                and (seg_yi / pct_based_yi > 3 or pct_based_yi / seg_yi > 3)
            )
            if exceeds_total or pct_mismatch:
                if pct_based_yi:
                    old_yi = seg_yi
                    seg_yi = pct_based_yi
                    reason = "超过总营收" if exceeds_total else f"与pct推算值({pct_based_yi}亿)差距>3x"
                    print(f"    [{symbol}] ⚠ 分部值 {old_yi}亿 {reason}，"
                          f"按 pct_of_total {pct}% 修正为 {seg_yi}亿")
                    segment["revenue_yi"] = seg_yi
                else:
                    print(f"    [{symbol}] ⚠ 分部值 {seg_yi}亿 异常且无 pct_of_total，丢弃",
                          file=sys.stderr)
                    seg_yi = None

        if seg_yi and seg_yi > 0:
            p["revenue_cny_yi"] = round(seg_yi, 1)
            p["revenue_usd_b"] = round(seg_yi * 1e8 / (usd_cny * 1e9), 2)
            p["revenue_total_cny_yi"] = total_cny_yi  # 保存总量
            p["revenue_is_segment"] = True
            p["segment_label"] = segment.get("name") or cfg.get("segment_label", "光通信分部")
            p["segment_pct"] = _safe(segment.get("pct_of_total"))
            p["segment_note"] = segment.get("note", "")
            p["segment_source"] = f"{fiscal_label} 年报 PDF + Claude Haiku 提取"

    return p


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="更新全球竞争格局同行数据")
    parser.add_argument(
        "--with-pdf", action="store_true",
        help="同时从年报 PDF 提取华工科技/东山精密光通信分部营收（季度人工触发）",
    )
    args = parser.parse_args()

    print(f"=== update_research_peers ({datetime.now(BJT):%Y-%m-%d %H:%M} BJT) ===")
    if args.with_pdf:
        print("  模式: 完整模式（含 PDF 分部提取）")
    else:
        print("  模式: 快速模式（API 数据，跳过 PDF 分部提取）")

    print("  拉取 USD/CNY 汇率...", flush=True)
    usd_cny = fetch_usd_cny()
    print(f"  USDCNY = {usd_cny}", flush=True)

    # 保留上次 PDF 分部提取结果（快速模式时沿用）
    existing_peers: dict[str, dict] = {}
    existing_json = DATA_DIR / "300308-peers.json"
    if existing_json.exists():
        try:
            old = json.loads(existing_json.read_text(encoding="utf-8"))
            for p in old.get("peers", []):
                existing_peers[p["key"]] = p
        except Exception:
            pass

    peers: list[dict] = []
    errors: list[str] = []

    # ── 旭创自身 ──
    try:
        p = load_innolight_peer(usd_cny)
        peers.append(p)
        print(f"  [300308] ¥{p['revenue_cny_yi']}亿 / ${p['revenue_usd_b']}B  "
              f"净利率={p['net_margin_pct']}%  毛利率={p['gross_margin_pct']}%")
    except Exception as e:
        errors.append(f"[innolight] {e}")
        print(f"ERROR [300308]: {e}", file=sys.stderr)

    # ── 海外上市公司 ──
    for cfg in _OVERSEAS_CONFIGS:
        try:
            p = fetch_overseas_peer(cfg)
            peers.append(p)
            seg_flag = " [分部]" if p.get("revenue_is_segment") else ""
            print(f"  [{cfg['ticker']}] ${p['revenue_usd_b']}B{seg_flag}  "
                  f"净利率={p['net_margin_pct']}%  毛利率={p['gross_margin_pct']}%  ({p['fiscal_year']})")
        except Exception as e:
            errors.append(f"[{cfg['ticker']}] {e}")
            print(f"ERROR [{cfg['ticker']}]: {e}", file=sys.stderr)

    # ── 纯光学 A 股同行 ──
    for cfg in _DOMESTIC_CONFIGS:
        try:
            p = fetch_domestic_peer(cfg, usd_cny)
            peers.append(p)
            print(f"  [{cfg['symbol']}] ¥{p['revenue_cny_yi']}亿 / ${p['revenue_usd_b']}B  "
                  f"净利率={p['net_margin_pct']}%  毛利率={p['gross_margin_pct']}%  ({p['fiscal_year']})")
        except Exception as e:
            errors.append(f"[{cfg['symbol']}] {e}")
            print(f"ERROR [{cfg['symbol']}]: {e}", file=sys.stderr)

    # ── 多元化同行（光通信分部）──
    for cfg in _PDF_SEGMENT_CONFIGS:
        key = cfg["key"]
        if args.with_pdf:
            print(f"  [{cfg['symbol']}] PDF 分部提取...", flush=True)
            try:
                p = fetch_segment_peer(cfg, usd_cny)
                peers.append(p)
                seg_flag = " [分部]" if p.get("revenue_is_segment") else " [总量]"
                print(f"  [{cfg['symbol']}] ¥{p['revenue_cny_yi']}亿{seg_flag}  "
                      f"净利率={p['net_margin_pct']}%  毛利率={p['gross_margin_pct']}%")
            except Exception as e:
                errors.append(f"[{cfg['symbol']}] {e}")
                print(f"ERROR [{cfg['symbol']}]: {e}", file=sys.stderr)
        else:
            # 快速模式：沿用上次 PDF 提取的结果
            if key in existing_peers:
                peers.append(existing_peers[key])
                src = existing_peers[key].get("segment_source", "缓存")
                print(f"  [{cfg['symbol']}] 沿用上次分部数据（{src}）")
            else:
                print(f"  [{cfg['symbol']}] 无缓存，跳过（运行 --with-pdf 初始化）")

    if errors:
        print(f"\n=== FAILED ({len(errors)}) ===", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    output = {
        "updated_at": datetime.now(BJT).isoformat(),
        "usd_cny": usd_cny,
        "peers": peers,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEPLOY_DATA_DIR.mkdir(parents=True, exist_ok=True)
    src_path = DATA_DIR / "300308-peers.json"
    dst_path = DEPLOY_DATA_DIR / "300308-peers.json"
    src_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(src_path, dst_path)

    print(f"\n  写出: {src_path} → {dst_path}")
    print(f"=== done ({len(peers)} peers, USDCNY={usd_cny}) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
