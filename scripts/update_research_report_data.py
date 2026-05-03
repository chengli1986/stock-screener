#!/usr/bin/env python3
"""
update_research_report_data.py — 从完整年度报告 PDF + API 提取财报补充数据

每个在 config/research_stocks.json 中注册的研究股票：
1. 通过 akshare stock_financial_benefit_ths 获取近 N 年研发费用
2. 通过巨潮资讯 API 查询最新完整年度报告 PDF 链接（非摘要版）
3. 用 pdfplumber 按数据类别抽取关键页面
4. 用 Claude Haiku 提取：员工结构、地区收入分布、各代产品出货情况
5. 写出 docs-site/data/{key}-report-data.json 并部署

触发时机：季报披露后人工运行，或每季度定时；数据更新比财务摘要低频。
脚本任意股票失败都 exit(1)，由 cron-wrapper 触发告警邮件。
"""

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
import anthropic
import pdfplumber
import requests

warnings.filterwarnings("ignore")

# ── paths ──────────────────────────────────────────────────────────────────────
REPO_DIR = pathlib.Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_DIR / "config" / "research_stocks.json"

DOCS_SITE_DIR = pathlib.Path(os.path.expanduser("~/docs-site"))
DATA_DIR = DOCS_SITE_DIR / "data"
DEPLOY_DATA_DIR = pathlib.Path("/var/www/overview/data")

BJT = timezone(timedelta(hours=8))
MAX_RD_YEARS = 6

# ── Anthropic client ───────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    # Fallback: read from openclaw env file (used when running via cron without ANTHROPIC_API_KEY set)
    _oc_env = pathlib.Path.home() / ".openclaw" / ".env"
    if _oc_env.exists():
        for _line in _oc_env.read_text().splitlines():
            if _line.startswith("ANTHROPIC_API_KEY="):
                ANTHROPIC_API_KEY = _line.split("=", 1)[1].strip()
                break

_claude = None


def _get_claude() -> anthropic.Anthropic:
    global _claude
    if _claude is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _claude


# ── helper ─────────────────────────────────────────────────────────────────────
def _safe(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


# ── R&D expense from akshare ───────────────────────────────────────────────────
def _parse_yi(val) -> float | None:
    """Parse '16.15亿' or '7762.14万' string to 亿元 float."""
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    try:
        if s.endswith("亿"):
            return float(s[:-1])
        if s.endswith("万"):
            return float(s[:-1]) / 10000
        return float(s) / 1e8
    except ValueError:
        return None


def fetch_rd_expenses(symbol: str) -> list[dict]:
    """Pull R&D expenses for last N years via akshare stock_financial_benefit_ths."""
    df = ak.stock_financial_benefit_ths(symbol=symbol, indicator="按年度")
    # Column '报告期' is integer year (2025, 2024, ...); '研发费用' is '16.15亿' string
    if "报告期" not in df.columns:
        return []

    rows = []
    for _, row in df.iterrows():
        year = str(row.get("报告期", "")).strip()
        if not year.isdigit() or len(year) != 4:
            continue

        rd = _parse_yi(row.get("研发费用"))
        revenue = _parse_yi(row.get("一、营业总收入"))
        if rd is None:
            continue
        entry: dict = {
            "year": year + "A",
            "rd_yi": round(rd, 2),
        }
        if revenue and revenue > 0:
            entry["rd_ratio_pct"] = round(rd / revenue * 100, 2)
        rows.append(entry)
        if len(rows) >= MAX_RD_YEARS:
            break

    return list(reversed(rows))  # ascending (old -> new)


# ── cninfo PDF discovery ───────────────────────────────────────────────────────
_CNINFO_PDF_BASE = "https://static.cninfo.com.cn/finalpage/"
_ANNUAL_FULL_PATTERN = re.compile(r"^\d{4}年年度报告$")
_EXCLUDE_PATTERN = re.compile(
    r"摘要|半年度|关于|更正|补充"
    r"|英文|取消|提示|差错"
)


def _find_annual_report_url(symbol: str, exchange: str) -> tuple[str, str, str] | None:
    """Find latest complete annual report PDF via akshare. Returns (pdf_url, title, date)."""
    start_date = (datetime.now(BJT) - timedelta(days=365 * 2)).strftime("%Y%m%d")
    end_date = datetime.now(BJT).strftime("%Y%m%d")

    df = ak.stock_zh_a_disclosure_report_cninfo(
        symbol=symbol,
        market="沪深京",
        keyword="年度报告",
        start_date=start_date,
        end_date=end_date,
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
        ann_id_match = re.search(r"announcementId=(\d+)", link)
        if not ann_id_match:
            continue
        ann_id = ann_id_match.group(1)
        pdf_url = f"{_CNINFO_PDF_BASE}{ann_time}/{ann_id}.PDF"
        return pdf_url, clean_title, ann_time

    return None


# ── PDF extraction ─────────────────────────────────────────────────────────────
# Category-based keyword matching ensures critical pages are always included
_PAGE_CATEGORIES: dict[str, list[str]] = {
    "employees": [
        "在职员工的数量合计",
        "在职员工",
        "员工数量合计",
        "专业构成类别",
        "生产人员",
        "从业人数",
    ],
    "rd_people": [
        "研发人员数量（人）",
        "研发人员数量",
    ],
    "geography": [
        "分地区",
        "境外收入",
        "按地区分",
    ],
    "production": [
        "出货量",
        "产能",
        "产量",
        "万只",
    ],
    "products": [
        "1.6T OSFP",
        "800G OSFP",
        "800G和1.6T",
    ],
}


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


def _extract_relevant_pages(pdf_path: pathlib.Path) -> tuple[str, int]:
    """Extract pages by category to ensure employee/geography/production data all covered."""
    total_pages = 0
    all_page_texts: list[tuple[int, str]] = []

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if text.strip():
                all_page_texts.append((i + 1, text))

    selected_idxs: set[int] = set()
    for cat, keywords in _PAGE_CATEGORIES.items():
        cat_pages = [
            (idx, t) for idx, t in all_page_texts
            if any(kw in t for kw in keywords)
        ]
        for idx, t in cat_pages[:2]:  # at most 2 pages per category
            selected_idxs.add(idx)

    selected = sorted(
        [(idx, t) for idx, t in all_page_texts if idx in selected_idxs],
        key=lambda x: x[0],
    )[:10]

    relevant_texts = [f"--- 第{idx}页 ---\n{t}" for idx, t in selected]
    return "\n\n".join(relevant_texts), total_pages


# ── Claude Haiku extraction ────────────────────────────────────────────────────
_EXTRACTION_PROMPT = (
    "以下是中际旭创 300308（高速光模块）"
    "年度报告部分文字内容。\n"
    "请从中提取以下信息，以 JSON 格式输出：\n\n"
    "1. employees: 员工总数(total)、研发人员数(rd)、"
    "生产人员数(production)，单位：人\n"
    "   - 若文中有截止/截至日期说明，写入 note 字段\n"
    "   - 若某项找不到，该字段设为 null\n\n"
    "2. geographic_revenue: 境外收入占比(overseas_pct，%)、"
    "境内收入占比(domestic_pct，%)\n"
    "   - 若文中标注年度，写入 year 字段\n"
    "   - 若找不到，该字段整体设为 null\n\n"
    "3. shipment_volumes: 各代产品出货情况\n"
    "   - description: 简洁描述（1-2句）\n"
    "   - items: 数组，每项含 gen(产品代别如'800G') + "
    "volume_desc(出货描述如'占比约65%')\n"
    "   - 若文中无定量数据，description 写定性描述，items 设为空数组\n\n"
    "只输出合法 JSON，不要任何注释或前缀说明文字。\n\n"
    "格式示例（参考，实际値按文中内容填写）：\n"
    '{"employees":{"total":10000,"rd":2000,"production":5000,"note":"截至2025年12月31日"},'
    '"geographic_revenue":{"overseas_pct":90.58,"domestic_pct":9.42,"year":"2025"},'
    '"shipment_volumes":{"description":"800G为主力产品，1.6T占比持续提升",'
    '"items":[{"gen":"800G","volume_desc":"占比约65%"},{"gen":"1.6T","volume_desc":"快速增长"}]}}\n\n'
    "PDF 年度报告文字内容：\n"
)


def _extract_with_llm(pdf_text: str) -> dict:
    """Call Claude Haiku to extract structured data from PDF text."""
    client = _get_claude()
    prompt = _EXTRACTION_PROMPT + pdf_text[:12000]

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()

    # Strip markdown code fence if present
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        raise ValueError(f"LLM returned unparseable response: {raw[:200]}")
    return json.loads(json_match.group())


# ── main builder ───────────────────────────────────────────────────────────────
def build_report_data(stock: dict) -> dict:
    symbol = stock["symbol"]
    exchange = stock["exchange"]

    print(f"  [{symbol}] 拉取研发费用 (akshare)...", flush=True)
    rd_expenses = fetch_rd_expenses(symbol)
    time.sleep(0.5)

    print(f"  [{symbol}] 查询巫潮完整年报 PDF 链接...", flush=True)
    pdf_info = _find_annual_report_url(symbol, exchange)
    if pdf_info is None:
        raise ValueError(f"[{symbol}] cninfo API 未找到年度报告 PDF")

    pdf_url, pdf_title, pdf_date = pdf_info
    print(f"  [{symbol}] 找到 PDF: {pdf_title} ({pdf_date})", flush=True)
    print(f"  [{symbol}]   URL: {pdf_url}", flush=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = pathlib.Path(tmpdir) / f"{symbol}-annual.pdf"
        print(f"  [{symbol}] 下载 PDF...", flush=True)
        _download_pdf(pdf_url, pdf_path)
        size_kb = pdf_path.stat().st_size // 1024
        print(f"  [{symbol}]   下载完成 ({size_kb} KB)", flush=True)

        print(f"  [{symbol}] 提取关键页面文字...", flush=True)
        pdf_text, total_pages = _extract_relevant_pages(pdf_path)

    print(f"  [{symbol}]   共 {total_pages} 页，提取到 {len(pdf_text)} 字符", flush=True)
    if not pdf_text.strip():
        raise ValueError(f"[{symbol}] PDF 无法提取到含关键词的文字")

    print(f"  [{symbol}] 调用 Claude Haiku 提取结构化数据...", flush=True)
    extracted = _extract_with_llm(pdf_text)
    print(f"  [{symbol}]   提取完成", flush=True)

    return {
        "symbol": symbol,
        "name": stock["name"],
        "updated_at": datetime.now(BJT).isoformat(),
        "report_source": {
            "type": "annual_report",
            "title": pdf_title,
            "url": pdf_url,
            "date": pdf_date,
            "pages": total_pages,
        },
        "rd_expenses": rd_expenses,
        "extracted": extracted,
    }


def write_and_deploy(key: str, data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEPLOY_DATA_DIR.mkdir(parents=True, exist_ok=True)

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    src = DATA_DIR / f"{key}-report-data.json"
    src.write_text(json_str, encoding="utf-8")

    dst = DEPLOY_DATA_DIR / f"{key}-report-data.json"
    shutil.copy2(src, dst)
    print(f"  [{key}] report-data 写出: {src} → {dst}", flush=True)


# ── entry point ────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"=== update_research_report_data ({datetime.now(BJT):%Y-%m-%d %H:%M} BJT) ===")

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
            data = build_report_data(stock)
            write_and_deploy(stock["snapshot_key"], data)
            rd = data["rd_expenses"][-1] if data["rd_expenses"] else {}
            emp = (data.get("extracted") or {}).get("employees") or {}
            geo = (data.get("extracted") or {}).get("geographic_revenue") or {}
            print(
                f"  [{symbol}] OK  "
                f"研发费用={rd.get('rd_yi')}亿({rd.get('rd_ratio_pct')}%)  "
                f"员工={emp.get('total')}人  "
                f"境外收入={geo.get('overseas_pct')}%"
            )
        except Exception as e:
            msg = f"[{symbol}] FAILED: {e}"
            print(f"ERROR: {msg}", file=sys.stderr)
            errors.append(msg)
        time.sleep(1)

    if errors:
        print(f"\n=== FAILED ({len(errors)}/{len(stocks)}) ===", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print(f"\n=== done ({len(stocks)} stocks updated) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
