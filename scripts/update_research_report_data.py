#!/usr/bin/env python3
"""
update_research_report_data.py — 从完整年度报告 PDF + API 提取财报补充数据

每个在 config/research_stocks.json 中注册的研究股票：
1. 通过 akshare stock_financial_benefit_ths 获取近 N 年研发费用
2. 通过巨潮资讯 API 查询最新完整年度报告 PDF 链接（非摘要版）
3. 用 pdfplumber 按数据类别抽取关键页面
4. 用 Claude (Max Plan) 提取：员工结构、地区收入分布、各代产品出货情况
5. 写出 docs-site/data/{key}-report-data.json 并部署

触发时机：每年 5 月 5 日自动运行（沪深交易所 4 月 30 日年报截止后 5 天），或年报季后人工运行。
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

# ── LLM via Claude Code Max Plan (no API billing) ────────────────────────────────
# 研报模块 LLM 统一走 Max 订阅，不用 ANTHROPIC_API_KEY 按量计费（用户明确要求）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _claude_max import call_claude_max  # noqa: E402


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


_RD_MAX_ATTEMPTS = 3
_RD_RETRY_WAIT_S = 5


def fetch_rd_expenses(symbol: str) -> list[dict]:
    """研发费用，带瞬时故障重试。

    实跑（2026-08-06 宁德 300750）：同花顺返回了非 JSON 响应，`JSONDecodeError`
    直接把整条腿判死；而同一接口几分钟前的探针是成功的 —— 瞬时抖动，不是没数据。
    这个脚本每年只跑一次，一次抖动毁掉的数据要等 12 个月才有下次机会。

    港股在 A 股接口上是**结构性**失败（`KeyError: 'flashData'`），重试必然再失败，
    直接上抛不浪费 3 轮等待。
    """
    last: Exception | None = None
    for attempt in range(1, _RD_MAX_ATTEMPTS + 1):
        try:
            return _fetch_rd_expenses_once(symbol)
        except KeyError:
            raise                      # 结构性（港股），重试无意义
        except Exception as e:
            last = e
            if attempt < _RD_MAX_ATTEMPTS:
                print(f"  [{symbol}]   研发费用第 {attempt} 次失败（{type(e).__name__}），"
                      f"{_RD_RETRY_WAIT_S}s 后重试", flush=True)
                time.sleep(_RD_RETRY_WAIT_S)
    raise last  # type: ignore[misc]


def _fetch_rd_expenses_once(symbol: str) -> list[dict]:
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

# 标题**以**「YYYY年年度报告」结尾即可，允许公司名前缀 —— 巨潮的标题格式各公司自定：
# 旭创/宁德/三环/风华/寒武纪/长光是「2025年年度报告」，
# 茅台是「贵州茅台2025年年度报告」，源杰是「陕西源杰半导体科技股份有限公司2025年年度报告」。
# 旧版锚在 `^\d{4}` 上，把带公司名的那批**静默漏掉**（实测漏 3/9）。
_ANNUAL_FULL_PATTERN = re.compile(r"\d{4}年年度报告$")
_EXCLUDE_PATTERN = re.compile(
    r"摘要|半年度|关于|更正|补充"
    r"|英文|取消|提示|差错"
)


def is_full_annual_report(title: str) -> bool:
    """是否为「全文版年度报告」（排除摘要 / 英文版 / 半年报 / 提示性公告）。"""
    if not title:
        return False
    clean = re.sub(r"<[^>]+>", "", title).strip()
    if not _ANNUAL_FULL_PATTERN.search(clean):
        return False
    return not _EXCLUDE_PATTERN.search(clean)


def annual_report_unavailable_reason(stock: dict) -> str | None:
    """结构性拿不到年报的原因；能拿则返回 None。

    目前只有一条结构性约束：巨潮 `market='沪深京'` 不含港股。
    次新股属于「查过之后才知道」，由 `classify_no_match` 判定，不在这里预判。
    """
    if str(stock.get("exchange", "")).upper() == "HK":
        return "港股，巨潮资讯（沪深京）不覆盖"
    return None


def is_empty_cninfo_result(err: Exception) -> bool:
    """该异常是否只是「巨潮返回空结果」的表现形态。

    pandas 对空 DataFrame 做列选择时报
    `None of [Index([...])] are in the [columns]`。按这个特征串识别，
    避免把别的 KeyError（真 bug）一起吞掉。
    """
    if not isinstance(err, KeyError):
        return False
    msg = str(err)
    return "are in the [columns]" in msg and "None of" in msg


def classify_no_match(announcements_found: int) -> str:
    """一条都没匹配上时，判断这是「本来就没有」还是「我们过滤错了」。

    ★这个区分是推广后能不能信任告警的关键：
    - 返回 0 条 → 该公司近两年确实没发过年报（次新股，如长鑫 2026-07-27 上市）→ unavailable
    - 返回 N 条却全被过滤掉 → 正是茅台/源杰/盛科此前的形态（正则锚点错）→ failure
      当成 unavailable 会把这类 bug 永久掩埋。
    """
    return "unavailable" if announcements_found == 0 else "failure"


def _find_annual_report_url(symbol: str, exchange: str) -> tuple[str, str, str] | None:
    """Find latest complete annual report PDF via akshare. Returns (pdf_url, title, date).

    Raises ValueError when cninfo returned announcements but none matched — that is a
    filtering bug, not an absence of data.
    """
    start_date = (datetime.now(BJT) - timedelta(days=365 * 2)).strftime("%Y%m%d")
    end_date = datetime.now(BJT).strftime("%Y%m%d")

    try:
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=symbol,
            market="沪深京",
            keyword="年度报告",
            start_date=start_date,
            end_date=end_date,
        )
    except KeyError as e:
        # akshare 对空结果**在返回前**就做了列选择并抛 KeyError —— 守卫必须包住调用本身，
        # 放在返回之后就永远轮不到（长鑫 688825 实测）。只吞「空结果」这一种形态。
        if is_empty_cninfo_result(e):
            return None
        raise
    if df is None or df.empty or "公告标题" not in df.columns:
        return None

    for _, row in df.iterrows():
        raw_title = row.get("公告标题", "")
        clean_title = re.sub(r"<[^>]+>", "", raw_title)
        ann_time = str(row.get("公告时间", ""))[:10]

        if not is_full_annual_report(clean_title):
            continue

        link = row.get("公告链接", "")
        ann_id_match = re.search(r"announcementId=(\d+)", link)
        if not ann_id_match:
            continue
        ann_id = ann_id_match.group(1)
        pdf_url = f"{_CNINFO_PDF_BASE}{ann_time}/{ann_id}.PDF"
        return pdf_url, clean_title, ann_time

    if classify_no_match(len(df)) == "failure":
        raise ValueError(
            f"[{symbol}] 巨潮返回 {len(df)} 条年度报告公告但无一匹配全文年报 —— "
            f"疑似标题过滤规则失效，请检查 is_full_annual_report()"
        )
    return None


# ── PDF extraction ─────────────────────────────────────────────────────────────
# Category-based keyword matching ensures critical pages are always included.
# 这里只放**所有行业年报都有**的类目；行业专有词（如光模块的 `1.6T OSFP`）
# 走 config 的 `report_keywords`，否则拿旭创的关键词去扫茅台年报只会命中无关页面。
_UNIVERSAL_PAGE_CATEGORIES: dict[str, list[str]] = {
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
        "销量",
    ],
}


def page_categories(stock: dict) -> dict[str, list[str]]:
    """通用类目 + 该股票在 config 里声明的行业专有关键词。"""
    cats = {k: list(v) for k, v in _UNIVERSAL_PAGE_CATEGORIES.items()}
    extra = stock.get("report_keywords") or []
    if extra:
        cats["products"] = list(extra)
    return cats


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


def _extract_relevant_pages(pdf_path: pathlib.Path, stock: dict) -> tuple[str, int]:
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
    for cat, keywords in page_categories(stock).items():
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


# ── Claude (Max Plan) extraction ────────────────────────────────────────────────────
def build_extraction_prompt(stock: dict) -> str:
    """按股票生成提取 prompt。

    ★不能复用写死旭创的那版：原文开头是「以下是中际旭创 300308（高速光模块）年度报告」，
    拿它去读茅台年报会把 LLM 引导去找根本不存在的光模块出货量。

    第 3 项也从「各代产品出货情况」放宽为公司实际披露的产销口径 —— 出货量对茅台无意义。
    """
    name = stock.get("name", "")
    symbol = stock.get("symbol", "")
    business = stock.get("business")
    head = (
        f"以下是 {name} {symbol}"
        + (f"（{business}）" if business else "")
        + "年度报告部分文字内容。\n"
    )
    return head + _EXTRACTION_BODY


_EXTRACTION_BODY = (
    "请从中提取以下信息，以 JSON 格式输出：\n\n"
    "1. employees: 员工总数(total)、研发人员数(rd)、"
    "生产人员数(production)，单位：人\n"
    "   - 若文中有截止/截至日期说明，写入 note 字段\n"
    "   - 若某项找不到，该字段设为 null\n\n"
    "2. geographic_revenue: 境外收入占比(overseas_pct，%)、"
    "境内收入占比(domestic_pct，%)\n"
    "   - 若文中标注年度，写入 year 字段\n"
    "   - 若找不到，该字段整体设为 null\n\n"
    "3. shipment_volumes: 该公司**实际披露的**产销/经营量指标\n"
    "   - description: 简洁描述（1-2句），用公司自己的口径\n"
    "   - items: 数组，每项含 gen(细分品类或产品代别) + volume_desc(数量/占比描述)\n"
    "   - **只写本报告中真实出现的指标**，不要套用其他行业的口径；\n"
    "     若文中无相关定量数据，description 写定性描述、items 设为空数组\n\n"
    "只输出合法 JSON，不要任何注释或前缀说明文字。\n\n"
    "格式示例（仅示意结构，数值与品类一律按文中实际内容填写）：\n"
    '{"employees":{"total":10000,"rd":2000,"production":5000,"note":"截至2025年12月31日"},'
    '"geographic_revenue":{"overseas_pct":90.58,"domestic_pct":9.42,"year":"2025"},'
    '"shipment_volumes":{"description":"<按文中口径描述>",'
    '"items":[{"gen":"<品类>","volume_desc":"<数量或占比>"}]}}\n\n'
    "PDF 年度报告文字内容：\n"
)


def _extract_with_llm(pdf_text: str, stock: dict) -> dict:
    """Extract structured data from PDF text via Claude Code Max Plan (no API billing)."""
    prompt = build_extraction_prompt(stock) + pdf_text[:12000]
    raw = call_claude_max(prompt, timeout=120)

    # Strip markdown code fence if present
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        raise ValueError(f"LLM returned unparseable response: {raw[:200]}")
    return json.loads(json_match.group())


# ── main builder ───────────────────────────────────────────────────────────────
def assemble_payload(stock: dict, rd_expenses: list[dict], report: dict | None,
                     unavailable_reason: str | None) -> dict:
    """把两条腿的结果组装成落盘 JSON，并显式记录本次到底拿到了什么。

    `coverage` 是给页面看的：没有数据时要能说清「为什么没有」，
    而不是显示一片空白让人以为抓漏了。
    """
    source = (report or {}).get("source")
    return {
        "symbol": stock["symbol"],
        "name": stock["name"],
        "updated_at": datetime.now(BJT).isoformat(),
        "report_source": source,
        "rd_expenses": rd_expenses or [],
        "extracted": (report or {}).get("extracted"),
        "coverage": {
            "has_annual_report": source is not None,
            "has_rd_expenses": bool(rd_expenses),
            "annual_report_note": unavailable_reason,
        },
    }


def coverage_summary(payload: dict) -> str:
    """人话说明本次缺了哪条腿；全都拿到则返回空串。

    实跑时摘要只印「部分数据缺失」，看不出缺的是研发费用还是年报 —— 等于没说。
    """
    cov = payload.get("coverage") or {}
    missing = []
    if not cov.get("has_annual_report"):
        missing.append("年报提取" + (f"（{cov['annual_report_note']}）"
                                    if cov.get("annual_report_note") else ""))
    if not cov.get("has_rd_expenses"):
        missing.append("研发费用")
    return "缺 " + "、".join(missing) if missing else ""


def fetch_annual_report(stock: dict) -> dict | None:
    """下载年报 PDF → 抽关键页 → LLM 结构化。结构性拿不到返回 None，真失败抛异常。"""
    symbol, exchange = stock["symbol"], stock["exchange"]

    reason = annual_report_unavailable_reason(stock)
    if reason:
        print(f"  [{symbol}] 跳过年报：{reason}", flush=True)
        return None

    print(f"  [{symbol}] 查询巨潮完整年报 PDF 链接...", flush=True)
    pdf_info = _find_annual_report_url(symbol, exchange)
    if pdf_info is None:
        print(f"  [{symbol}] 巨潮近两年无年度报告公告（次新股）", flush=True)
        return None

    pdf_url, pdf_title, pdf_date = pdf_info
    print(f"  [{symbol}] 找到 PDF: {pdf_title} ({pdf_date})", flush=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = pathlib.Path(tmpdir) / f"{symbol}-annual.pdf"
        print(f"  [{symbol}] 下载 PDF...", flush=True)
        _download_pdf(pdf_url, pdf_path)
        size_kb = pdf_path.stat().st_size // 1024
        print(f"  [{symbol}]   下载完成 ({size_kb} KB)", flush=True)

        print(f"  [{symbol}] 提取关键页面文字...", flush=True)
        pdf_text, total_pages = _extract_relevant_pages(pdf_path, stock)

    print(f"  [{symbol}]   共 {total_pages} 页，提取到 {len(pdf_text)} 字符", flush=True)
    if not pdf_text.strip():
        raise ValueError(f"[{symbol}] PDF 无法提取到含关键词的文字")

    print(f"  [{symbol}] 调用 Claude (Max Plan) 提取结构化数据...", flush=True)
    extracted = _extract_with_llm(pdf_text, stock)
    print(f"  [{symbol}]   提取完成", flush=True)

    return {
        "source": {
            "type": "annual_report",
            "title": pdf_title,
            "url": pdf_url,
            "date": pdf_date,
            "pages": total_pages,
        },
        "extracted": extracted,
    }


def build_report_data(stock: dict) -> dict:
    """两条腿独立降级：研发费用（同花顺）与年报提取（巨潮+LLM）互不拖累。

    长鑫就是「有研发费用、无年报」的典型 —— 招股书的历史研发数据已进同花顺，
    但上市不满一年、首份年报要等 2027-04。旧实现会因后者失败而丢掉前者。
    """
    symbol = stock["symbol"]

    print(f"  [{symbol}] 拉取研发费用 (akshare)...", flush=True)
    try:
        rd_expenses = fetch_rd_expenses(symbol)
    except Exception as e:
        # 港股在同花顺 A 股接口上必然失败（KeyError: 'flashData'），不该拖垮年报那条腿
        print(f"  [{symbol}]   研发费用不可用：{type(e).__name__}: {e}", flush=True)
        rd_expenses = []
    time.sleep(0.5)

    report = fetch_annual_report(stock)
    reason = None
    if report is None:
        reason = (annual_report_unavailable_reason(stock)
                  or "上市不满一年，巨潮近两年无年度报告")

    return assemble_payload(stock, rd_expenses, report, reason)


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
    partial: list[str] = []

    for stock in stocks:
        symbol = stock["symbol"]
        try:
            data = build_report_data(stock)
            write_and_deploy(stock["snapshot_key"], data)
            rd = data["rd_expenses"][-1] if data["rd_expenses"] else {}
            emp = (data.get("extracted") or {}).get("employees") or {}
            geo = (data.get("extracted") or {}).get("geographic_revenue") or {}
            cov = data["coverage"]
            print(
                f"  [{symbol}] OK  "
                f"研发费用={rd.get('rd_yi')}亿({rd.get('rd_ratio_pct')}%)  "
                f"员工={emp.get('total')}人  "
                f"境外收入={geo.get('overseas_pct')}%"
            )
            gap = coverage_summary(data)
            if gap:
                partial.append(f"[{symbol}] {stock['name']}：{gap}")
        except Exception as e:
            msg = f"[{symbol}] FAILED: {e}"
            print(f"ERROR: {msg}", file=sys.stderr)
            errors.append(msg)
        time.sleep(1)

    # 结构性不可用（港股 / 次新股）只播报不告警 —— 若混进 exit(1)，
    # 每年 5-05 必然发一封「失败」邮件，久了这个告警就没人看了。
    if partial:
        print(f"\n=== 部分覆盖 ({len(partial)}/{len(stocks)}，属预期，不告警) ===")
        for p in partial:
            print(f"  {p}")

    if errors:
        print(f"\n=== FAILED ({len(errors)}/{len(stocks)}) ===", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print(f"\n=== done ({len(stocks)} stocks updated) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
