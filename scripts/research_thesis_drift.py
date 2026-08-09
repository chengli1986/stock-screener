#!/usr/bin/env python3
"""research_thesis_drift.py — 研报正文的估值结论是否已被现价推翻

## 问题（未决问题清单 ③）

研报正文里写着「当前 ~44x 已高于乐观情景上沿（35x）」这类**判断句**。数字是写死的，
价格却每天在动。原本靠周五 narrative agent 按 12% 价格 gate 重写正文，
该 cron 已于 2026-08-07 暂停 —— 此后没有任何机制会发现结论已经不成立。
`verify_report_js.py` 只查 id/字段一致性，查不出「数字过时」。

2026-08-09 实测：旭创正文断言「高于乐观上沿 35x」，实际 2026E PE 已回落到 34.9x，
**结论反了**；寒武纪断言「高于三情景目标区间（100/120/150）」，实际 137.7x
已落回 120–150 档内。两页的投资判断都在讲一件不再成立的事。

## 为什么不扫正文里的所有数字

11 页正文共 552 处「N倍/Nx」，绝大多数是产能倍数、同业倍数、情景目标倍数、
带日期的历史锚点——都不该被订正。全扫＝噪音，两周后没人看
（与背离告警同一教训，见 tests/test_divergence_alert_transitions.py）。
改为**只登记每页真正断言「当前估值处在哪个位置」的那一句**，逐股手工确认，
登记表在 `docs-site/data/report-thesis.json`。

## 判定用「第几档区间」而不是档位名称

PS 情景表方向是反的（营收假设越低 → forward PS 越高，保守档数值最大）。
只要比较「现值落在第几档区间」与「正文断言值落在第几档区间」，就不必关心
保守/乐观哪头大，也不用解析中文档位名。

## 只在状态**变化**时告警

「结论已反」是持续状态，天天报等于自我废弃（②的教训）。状态存
`docs-site/data/thesis-drift-state.json`，仅在跨档/回归时报一次。
"""
import json
import pathlib
import re
from datetime import date

DOCS_DATA_DIR = pathlib.Path.home() / "docs-site" / "data"
DOCS_PAGES_DIR = pathlib.Path.home() / "docs-site" / "pages"
THESIS_FILE = DOCS_DATA_DIR / "report-thesis.json"
STATE_FILE = DOCS_DATA_DIR / "thesis-drift-state.json"

METRIC_FIELD = {"pe": "pe_estimates", "ps": "ps_estimates"}


def _plain(html: str) -> str:
    """去标签去空白，用于在页面里定位登记的原句（原句常被 <span> 切断）。"""
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", re.sub(
        r"<script.*?</script>", "", html, flags=re.S)))


def claim_present(page_html: str, claim: str) -> bool:
    return re.sub(r"\s+", "", claim) in _plain(page_html)


def zone(value: float, bands: list) -> int:
    """现值落在第几档区间：0 = 低于最低档，len(bands) = 高于最高档。

    bands 需升序。用序号而非档位名，PS 情景表方向相反时同样适用。
    """
    return sum(1 for b in bands if value > b)


def zone_label(z: int, bands: list, labels: list | None) -> str:
    names = labels or [f"{b}x" for b in bands]
    if z == 0:
        return f"低于最低档（{names[0]}）"
    if z == len(bands):
        return f"高于最高档（{names[-1]}）"
    return f"介于 {names[z - 1]} 与 {names[z]} 之间"


def live_multiple(snapshot: dict, metric: str, year: str):
    """快照里的现值倍数；字段缺失/该年份没算出来时返回 None（不当成 0）。"""
    return (snapshot.get(METRIC_FIELD[metric]) or {}).get(year)


def evaluate(code: str, entry: dict, snapshot: dict, default_drift_pct: float) -> dict | None:
    """比对单只标的。返回 None = 无需关注；否则给出 status 与说明。"""
    if entry.get("skip"):
        return None
    metric, year = entry["metric"], entry["year"]
    live = live_multiple(snapshot, metric, year)
    if live is None:
        # 快照没有该年份倍数（视界滚动过了，或分母缺失）——这是配置漂移，要报
        return {"code": code, "name": entry["name"], "page": entry.get("page"),
                "status": "missing",
                "what": f"登记表按 {year} {metric.upper()} 比对，快照里没有这个年份",
                "detail": "登记表的 year 需要跟上滚动视界，或该股分母缺失",
                "claim": entry.get("claim", "")}

    asserted = entry["asserted"]
    bands = entry.get("bands")
    if bands:
        z_live, z_said = zone(live, bands), zone(asserted, bands)
        if z_live == z_said:
            return None
        return {"code": code, "name": entry["name"], "page": entry.get("page"),
                "status": f"zone:{z_live}",
                "what": "正文的估值结论已被现价推翻",
                "detail": (f"正文按 {asserted:g}x 判定「{zone_label(z_said, bands, entry.get('band_labels'))}」，"
                           f"现价对应 {year} {metric.upper()} {live:g}x，实际"
                           f"「{zone_label(z_live, bands, entry.get('band_labels'))}」"),
                "claim": entry.get("claim", "")}

    drift = (live - asserted) / asserted * 100
    if abs(drift) < default_drift_pct:
        return None
    return {"code": code, "name": entry["name"], "page": entry.get("page"),
            "status": "drift:high" if drift > 0 else "drift:low",
            "what": "正文写死的估值数字已明显偏离",
            "detail": (f"正文写 {asserted:g}x，现价对应 {year} {metric.upper()} "
                       f"{live:g}x（{drift:+.0f}%）"),
            "claim": entry.get("claim", "")}


def load_state(path: pathlib.Path = None) -> dict:
    p = path or STATE_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict, path: pathlib.Path = None) -> None:
    (path or STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def transitions(findings: list, prev: dict) -> tuple[list, dict]:
    """只保留状态**变化**的条目；同时返回要落盘的新状态。

    - 新出现问题 / 换了一档  → 报
    - 状态没变              → 静默（持续状态天天报＝自我废弃）
    - 问题消失              → 报「已恢复」（正文重新说得通了，同样值得知道）
    """
    now = {f["code"]: f["status"] for f in findings}
    changed = [f for f in findings if prev.get(f["code"]) != f["status"]]
    for code, old in prev.items():
        if code not in now and old != "aligned":
            changed.append({"code": code, "name": code, "page": None,
                            "status": "aligned", "what": "正文结论重新与现价一致",
                            "detail": "此前告警的偏离已消失", "claim": ""})
    return changed, now


def collect(data_dir: pathlib.Path = None, thesis_file: pathlib.Path = None,
            pages_dir: pathlib.Path = None) -> list:
    """扫全部登记标的，返回当前所有偏离（未做变化过滤）。"""
    data = data_dir or DOCS_DATA_DIR
    pages = pages_dir or DOCS_PAGES_DIR
    tf = thesis_file or THESIS_FILE
    if not tf.exists():
        return []
    cfg = json.loads(tf.read_text())
    default_drift = cfg.get("default_drift_pct", 20)
    out = []
    for code, entry in (cfg.get("stocks") or {}).items():
        # 页面被改过而登记表没跟上 → 之后一直拿旧句子跟现价比，静默失效。
        # 这类「登记表自己烂掉」正是本管线反复栽跟头的那一类，必须报出来。
        page = pages / (entry.get("page") or "").lstrip("/")
        if entry.get("claim") and page.is_file() and not claim_present(
                page.read_text(encoding="utf-8"), entry["claim"]):
            out.append({"code": code, "name": entry["name"], "page": entry.get("page"),
                        "status": "claim-gone", "what": "登记表与页面脱节",
                        "detail": "登记的正文原句在页面里已找不到——正文被改过，"
                                  "report-thesis.json 需要同步更新",
                        "claim": entry["claim"]})
            continue
        snap_file = data / f"{code}-snapshot.json"
        if not snap_file.exists():
            continue
        r = evaluate(code, entry, json.loads(snap_file.read_text()), default_drift)
        if r:
            out.append(r)
    return out


def build_html(changed: list, today: date) -> str:
    """告警正文。要说清**改哪一页的哪一句**，否则读者只知道「有问题」。"""
    if not changed:
        return ""
    rows = []
    for f in changed:
        link = (f"<a href='https://docs.sinostor.com.cn{f['page']}'>{f['name']}</a>"
                if f.get("page") else f["name"])
        quote = (f"<div style='color:#666;font-size:12px;margin-top:4px'>正文原句：「{f['claim']}」</div>"
                 if f.get("claim") else "")
        rows.append(
            f"<tr><td style='padding:8px 10px;border-bottom:1px solid #eee;vertical-align:top'>{link}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #eee'>"
            f"<b>{f['what']}</b><div style='margin-top:2px'>{f['detail']}</div>{quote}</td></tr>")
    return (
        f"<h3 style='margin-top:22px'>📝 研报正文与现价不一致（{today} BJT）</h3>"
        "<p>下列研报的<b>估值判断句</b>已经和现价对不上。正文数字不会自动更新——"
        "请改写对应段落，或恢复 narrative 周更 cron 让它代劳。</p>"
        "<table style='border-collapse:collapse;font-size:13px'>"
        "<tr><th style='text-align:left;padding:6px 10px;border-bottom:2px solid #333'>研报</th>"
        "<th style='text-align:left;padding:6px 10px;border-bottom:2px solid #333'>问题</th></tr>"
        + "".join(rows) + "</table>"
        "<p style='color:#888;font-size:12px'>登记表 docs-site/data/report-thesis.json；"
        "仅在结论跨档或回归时提醒一次，持续状态不重复打扰。</p>")
