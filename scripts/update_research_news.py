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


# ── 新闻攒历史 ───────────────────────────────────────────────────────────────


def merge_news_history(key: str, fresh: list[dict], data_dir: Path,
                       now: str | None = None, days: int = WINDOW_DAYS) -> list[dict]:
    """按 url 去重追加进 `{key}-news-raw.jsonl`，回读窗口内的条目。

    ★窗口只影响返回值，**不删磁盘上的历史**——删了就再也拼不回更长的时间线。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{key}-news-raw.jsonl"

    rows: list[dict] = []
    seen: set = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
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
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")

    cutoff = _cutoff(now, days)
    return [r for r in rows if str(r.get("date") or "") >= cutoff]


def _cutoff(now: str | None, days: int) -> str:
    base = (datetime.strptime(now, "%Y-%m-%d").date() if now
            else datetime.now(BJT).date())
    return (base - timedelta(days=days)).strftime("%Y-%m-%d")


# ── 合成 ─────────────────────────────────────────────────────────────────────


def build_payload(key: str, name: str, announcements: list[dict],
                  news: list[dict], now: str | None = None) -> dict:
    """分层 + 聚合 + 排序，产出页面直接消费的结构。"""
    cutoff = _cutoff(now, WINDOW_DAYS)
    items = [x for x in (list(announcements) + list(news))
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

    ann_in_window = [a for a in announcements if str(a.get("date") or "") >= cutoff]
    return {
        "symbol": key,
        "name": name,
        "window_days": WINDOW_DAYS,
        "as_of": now or datetime.now(BJT).strftime("%Y-%m-%d"),
        "groups": groups,
        # ★空态要显式标出来：实测长光华芯近 30 天 0 条公告。
        # 页面据此说明「近 30 天无公告披露」，而不是留白。
        "announcements_empty": len(ann_in_window) == 0,
        "is_empty": len(groups) == 0,
    }


# ── 落盘 ─────────────────────────────────────────────────────────────────────


def write_and_deploy(key: str, payload: dict, data_dir: Path,
                     deploy_dir: Path | None) -> Path | None:
    """写 `{key}-news.json` 并部署。

    ★空 payload 不覆盖已有文件——抓取失败时照常写盘，会把昨天好的数据换成空壳。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{key}-news.json"
    if payload.get("is_empty") and path.exists():
        print(f"  [{key}] 本次无内容，保留既有文件（不覆盖）")
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
        if s.get("exchange") == "HK":
            continue          # 港股在 Task 4 接入
        try:
            anns = fetch_cninfo_announcements(s["symbol"])
        except Exception as e:  # noqa: BLE001
            print(f"WARN: [{key}] 公告抓取失败: {type(e).__name__}: {e}", file=sys.stderr)
            anns = []
            errors.append(f"{key}:{type(e).__name__}")
        try:
            fresh = fetch_em_news(s["symbol"])
        except Exception as e:  # noqa: BLE001
            print(f"WARN: [{key}] 新闻抓取失败: {type(e).__name__}: {e}", file=sys.stderr)
            fresh = []
            errors.append(f"{key}:{type(e).__name__}")
        news = merge_news_history(key, fresh, DATA_DIR)
        payload = build_payload(key, name, anns, news)
        write_and_deploy(key, payload, DATA_DIR, DEPLOY_DATA_DIR)
        print(f"  [{key}] ✓ {name}  公告 {len(anns)} 条 / 新闻 {len(news)} 条"
              f"  分 {len(payload['groups'])} 层")
    if errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
