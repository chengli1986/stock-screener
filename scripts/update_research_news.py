#!/usr/bin/env python3
"""update_research_news.py — 研报页「相关新闻」章节的数据采集

设计见 docs-site/docs/superpowers/specs/2026-08-11-research-news-section-design.md

## 两层数据，两种取法（这是本脚本唯一的结构性决定）

公告   巨潮/披露易支持日期区间  → 每次重拉近 30 天，无历史包袱
新闻   东财固定只给 10 条       → 每日抓、按 url 去重追加 jsonl，回读近 30 天

实测盛科通信 10 条新闻只覆盖 4 天——不攒历史就拼不出用户要的「近一个月」。
而公告不必攒：日期区间接口一次就给全，攒反而会积累一份可能已经歪掉的副本。
"""

import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))
REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "research_stocks.json"
DATA_DIR = REPO.parent / "docs-site" / "data"
DEPLOY_DATA_DIR = Path("/var/www/overview/data")
WINDOW_DAYS = 30

sys.path.insert(0, str(Path(__file__).resolve().parent))
from news_rules import LAYER_LABEL, LAYER_ORDER, classify, group_events  # noqa: E402


def call_with_timeout(fn, timeout_s: int, label: str = ""):
    """daemon 线程 + join 硬超时。akshare/yfinance 内部 requests 普遍不带 timeout。"""
    import threading

    box: dict = {}

    def runner():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"{label} exceeded {timeout_s}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


# ── 采集 ─────────────────────────────────────────────────────────────────────


def fetch_cninfo_announcements(code: str, days: int = WINDOW_DAYS) -> list[dict]:
    """巨潮按代码 + 日期区间拉公告。港股不覆盖（巨潮 market='沪深京'）。"""
    def _work():
        import akshare as ak

        end = date.today()
        start = end - timedelta(days=days)
        return ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, market="沪深京",
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))

    df = call_with_timeout(_work, 60, label=f"cninfo[{code}]")
    if df is None or df.empty:
        return []
    return [{"kind": "announcement",
             "title": str(r["公告标题"]).strip(),
             "date": str(r["公告时间"])[:10],
             "url": str(r["公告链接"]).strip(),
             "category": None}
            for _, r in df.iterrows()]


def fetch_em_news(code: str) -> list[dict]:
    """东财个股新闻，固定返回最新 10 条。"""
    def _work():
        import akshare as ak

        return ak.stock_news_em(symbol=code)

    df = call_with_timeout(_work, 60, label=f"em-news[{code}]")
    if df is None or df.empty:
        return []
    return [{"kind": "news",
             "title": str(r["新闻标题"]).strip(),
             "date": str(r["发布时间"])[:10],
             "url": str(r["新闻链接"]).strip(),
             "source": str(r.get("文章来源") or "").strip()}
            for _, r in df.iterrows()]


# ── 港股 ─────────────────────────────────────────────────────────────────────
#
# 巨潮 market='沪深京' 不含港股，港股走港交所披露易官方查询接口。
# 需先把股票代码换成内部 stockId（智谱 02513 → 1000286748），该映射写进
# config/research_stocks.json 的 hkex_stock_id 缓存，避免每次多一次请求。

_HKEX_BASE = "https://www1.hkexnews.hk"
_HKEX_SEARCH = _HKEX_BASE + "/search/titleSearchServlet.do"
_HKEX_HEADERS = {"User-Agent": "Mozilla/5.0"}


def parse_hkex_rows(rows: list[dict]) -> list[dict]:
    """披露易返回行 → 统一结构。

    ★日期是 DD/MM/YYYY。弄反会让全部条目掉出 30 天窗口——静默失败，页面只是空着。
    ★SHORT_TEXT 是官方分类（月報表 / 公告及通告 - [配售]），比猜标题可靠，
      带进 category 供 classify() 优先使用。
    """
    out = []
    for r in rows or []:
        raw_date = str(r.get("DATE_TIME") or "").strip()
        try:
            d = datetime.strptime(raw_date.split()[0], "%d/%m/%Y").strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            continue
        link = str(r.get("FILE_LINK") or "").strip()
        out.append({
            "kind": "announcement",
            "title": str(r.get("TITLE") or "").strip(),
            "date": d,
            "url": (_HKEX_BASE + link) if link.startswith("/") else link,
            "category": str(r.get("SHORT_TEXT") or "").replace("<br/>", "").strip(),
        })
    return out


def fetch_hkex_announcements(stock_id: str, days: int = WINDOW_DAYS) -> list[dict]:
    """按港交所内部 stockId 拉近 N 天公告。自带 timeout，不走 call_with_timeout。"""
    import requests

    end = date.today()
    start = end - timedelta(days=days)
    params = {"sortDir": "0", "sortByOptions": "DateTime", "category": "0",
              "market": "SEHK", "stockId": str(stock_id), "documentType": "-1",
              "fromDate": start.strftime("%Y%m%d"), "toDate": end.strftime("%Y%m%d"),
              "title": "", "searchType": "1", "t1code": "-2", "t2Gcode": "-2",
              "t2code": "-2", "rowRange": "100", "lang": "ZH"}
    r = requests.get(_HKEX_SEARCH, params=params, headers=_HKEX_HEADERS, timeout=30)
    r.raise_for_status()
    return parse_hkex_rows(json.loads(json.loads(r.text)["result"]))


def fetch_yf_news(ticker: str) -> list[dict]:
    """yfinance 的 news 字段。⚠ 它按标签松散关联，会有话题漂移
    （实测智谱的结果里混进过 BNB 币），分层时靠 classify 兜不住——
    这是港股新闻层质量明显低于 A 股的已知代价。"""
    def _work():
        import yfinance as yf

        return yf.Ticker(ticker).news or []

    raw = call_with_timeout(_work, 60, label=f"yf-news[{ticker}]") or []
    out = []
    for x in raw:
        c = x.get("content") or x
        pub = str(c.get("pubDate") or "")[:10]
        if not pub:
            continue
        out.append({"kind": "news",
                    "title": str(c.get("title") or "").strip(),
                    "date": pub,
                    "url": str((c.get("canonicalUrl") or {}).get("url")
                               or c.get("link") or "").strip(),
                    "source": str((c.get("provider") or {}).get("displayName") or "")})
    return out


# ── 新闻攒历史 ───────────────────────────────────────────────────────────────


def merge_news_history(key: str, fresh: list[dict], data_dir: Path,
                       now: str | None = None, days: int = WINDOW_DAYS) -> list[dict]:
    """按 url 去重追加进 `{key}-news-raw.jsonl`，回读窗口内的条目。

    ★窗口只影响返回值，**不删磁盘上的历史**——删了就再也拼不回更长的时间线。
    ★这份 jsonl 全系统唯一不可重建（东财只给最新 10 条），因此写盘用
      「写临时文件 + `os.replace()`」原子替换，绝不半路截断成坏文件。
    ★损坏的单行只丢它自己，不静默——打 WARN 到 stderr，其余好数据全保留。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{key}-news-raw.jsonl"
    if path.is_symlink():
        # os.replace 会把符号链接整个换成普通文件；先解析到真实路径，
        # 后续读写都对真实文件操作，不破坏链接本身。
        path = Path(os.path.realpath(path))

    rows: list[dict] = []
    seen: set = set()
    if path.exists():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"WARN: [{key}] news-raw.jsonl 第{lineno}行 JSON 解析失败，跳过: {e}",
                      file=sys.stderr)
                continue
            if not isinstance(r, dict):
                print(f"WARN: [{key}] news-raw.jsonl 第{lineno}行不是对象，跳过: {r!r}",
                      file=sys.stderr)
                continue
            rows.append(r)
            seen.add(r.get("url"))

    added = 0
    for it in fresh:
        if not it.get("url") or it["url"] in seen:
            continue
        seen.add(it["url"])
        rows.append(it)
        added += 1
    if added:
        rows.sort(key=lambda r: str(r.get("date") or ""))
        tmp = path.parent / f"{path.name}.tmp{os.getpid()}"
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                       encoding="utf-8")
        os.replace(tmp, path)

    cutoff = _cutoff(now, days)
    return [r for r in rows if str(r.get("date") or "") >= cutoff]


def _cutoff(now: str | None, days: int) -> str:
    base = (datetime.strptime(now, "%Y-%m-%d").date() if now
            else datetime.now(BJT).date())
    return (base - timedelta(days=days)).strftime("%Y-%m-%d")


# ── 合成 ─────────────────────────────────────────────────────────────────────


def build_payload(key: str, name: str, announcements: list[dict] | None,
                  news: list[dict], now: str | None = None) -> dict:
    """分层 + 聚合 + 排序，产出页面直接消费的结构。

    ``announcements=None`` 是「本次抓取失败，公告状况未知」的哨兵，与
    ``announcements=[]``（抓取成功、确认窗口内 0 条）**语义不同**：前者绝不能
    写成 `announcements_empty=True`，那会让页面打印「近 30 天无公告披露」这句
    假话——真相是我们根本没拿到数据，不是公司真的没有公告。
    """
    cutoff = _cutoff(now, WINDOW_DAYS)
    announcements_error = announcements is None
    ann_list = announcements if announcements is not None else []
    items = [x for x in (list(ann_list) + list(news))
             if str(x.get("date") or "") >= cutoff]

    buckets: dict = {layer: [] for layer in LAYER_ORDER}
    for it in items:
        buckets[classify(it)].append(it)

    groups = []
    for layer in LAYER_ORDER:
        rows = buckets[layer]
        if not rows:
            continue
        rows = group_events(sorted(rows, key=lambda x: str(x.get("date") or ""), reverse=True))
        groups.append({"layer": layer, "label": LAYER_LABEL[layer], "items": rows})

    ann_in_window = [a for a in ann_list if str(a.get("date") or "") >= cutoff]
    return {
        "symbol": key,
        "name": name,
        "window_days": WINDOW_DAYS,
        "as_of": now or datetime.now(BJT).strftime("%Y-%m-%d"),
        "groups": groups,
        # ★空态要显式标出来：实测长光华芯近 30 天 0 条公告。
        # 页面据此说明「近 30 天无公告披露」，而不是留白。
        # 但只在抓取**成功**的前提下才成立——见 announcements_error。
        "announcements_empty": (not announcements_error) and len(ann_in_window) == 0,
        "announcements_error": announcements_error,
        "is_empty": len(groups) == 0,
    }


# ── 落盘 ─────────────────────────────────────────────────────────────────────


def write_and_deploy(key: str, payload: dict, data_dir: Path,
                     deploy_dir: Path | None) -> Path | None:
    """写 `{key}-news.json` 并部署。

    两条「不覆盖」守卫：
    ★ 空 payload 不覆盖已有文件——抓取失败时照常写盘，会把昨天好的数据换成空壳。
    ★ `announcements_error` 为真也不覆盖——哪怕新闻抓取成功、payload 整体不算
      「空」，写出去也会把 `announcements_empty` 定死成一句假话，还顺手抹掉
      昨天那份真实的公告数据。设计文档第 6 节：「某一层抓取失败 → 不覆盖已有
      数据」，这里补的正是「公告空但新闻不空」这个比全空常见得多的场景。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{key}-news.json"
    if (payload.get("is_empty") or payload.get("announcements_error")) and path.exists():
        reason = "本次无内容" if payload.get("is_empty") else "公告抓取失败"
        print(f"  [{key}] {reason}，保留既有文件（不覆盖）")
        return None
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    if deploy_dir and Path(deploy_dir).is_dir():
        shutil.copy2(path, Path(deploy_dir) / path.name)
    return path


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    stocks = json.loads(CONFIG.read_text(encoding="utf-8"))
    errors = []
    for s in stocks:
        key = s.get("snapshot_key") or s["symbol"]
        name = s.get("name")
        is_hk = s.get("exchange") == "HK"
        # ★整只股票包一层 try/except：一只失败不该拖垮其余——实测过历史
        # jsonl 里一行合法 JSON 但非 dict（如裸数字 123）会让 `.get()` 抛
        # AttributeError，若不隔离，后面的股票全部不处理。
        try:
            if is_hk:
                sid = s.get("hkex_stock_id")
                if not sid:
                    # 缺映射＝公告层不可用，与抓取失败同样处理：哨兵 None，
                    # 绝不是 []（那会让 build_payload 断言「确认 0 条」）。
                    print(f"WARN: [{key}] 缺 hkex_stock_id，公告层标记为失败",
                          file=sys.stderr)
                    anns = None
                    errors.append(f"{key}:公告:缺hkex_stock_id")
                else:
                    try:
                        anns = fetch_hkex_announcements(sid)
                    except Exception as e:  # noqa: BLE001
                        print(f"WARN: [{key}] 披露易抓取失败: {type(e).__name__}: {e}",
                              file=sys.stderr)
                        anns = None
                        errors.append(f"{key}:公告:{type(e).__name__}")
                try:
                    fresh = fetch_yf_news(s.get("yf_ticker")
                                          or f"{s['symbol'].lstrip('0')}.HK")
                except Exception as e:  # noqa: BLE001
                    print(f"WARN: [{key}] yfinance 新闻失败: {type(e).__name__}: {e}",
                          file=sys.stderr)
                    fresh = []
                    errors.append(f"{key}:新闻:{type(e).__name__}")
            else:
                try:
                    anns = fetch_cninfo_announcements(s["symbol"])
                except Exception as e:  # noqa: BLE001
                    print(f"WARN: [{key}] 公告抓取失败: {type(e).__name__}: {e}", file=sys.stderr)
                    # 哨兵 None：与「抓取成功、确认 0 条」区分，见 build_payload。
                    anns = None
                    errors.append(f"{key}:公告:{type(e).__name__}")
                try:
                    fresh = fetch_em_news(s["symbol"])
                except Exception as e:  # noqa: BLE001
                    print(f"WARN: [{key}] 新闻抓取失败: {type(e).__name__}: {e}", file=sys.stderr)
                    fresh = []     # 新闻走 merge_news_history 回读历史，不会说谎
                    errors.append(f"{key}:新闻:{type(e).__name__}")
            news = merge_news_history(key, fresh, DATA_DIR)
            payload = build_payload(key, name, anns, news)
            write_and_deploy(key, payload, DATA_DIR, DEPLOY_DATA_DIR)
            ann_str = "抓取失败" if anns is None else f"{len(anns)} 条"
            tag = "（港股）" if is_hk else ""
            print(f"  [{key}] ✓ {name}{tag}  公告 {ann_str} / 新闻 {len(news)} 条"
                  f"  分 {len(payload['groups'])} 层")
        except Exception as e:  # noqa: BLE001
            msg = f"{key}:{type(e).__name__}"
            print(f"ERROR: [{key}] {name} FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            errors.append(msg)
            continue

    if errors:
        print(f"\n=== FAILED ({len(errors)}) ===", file=sys.stderr)
        for msg in errors:
            print(f"  {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
