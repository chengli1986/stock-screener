#!/usr/bin/env python3
"""
research_data_health.py — 研报数据新鲜度健康检查（防静默失败）

研报数据 cron（research-snapshots / research-peers-market / research-financials）只在
脚本报错退出时由 cron-wrapper 告警；若上游（akshare/腾讯）返回空或旧数据但脚本仍 exit 0，
研报会静默显示陈旧数据、无人知晓。本脚本扫 docs-site/data 各数据文件的时间戳，
超过新鲜度阈值即发告警邮件。

检查项：
  - 每个 research_stocks.json 注册股票：
      {key}-snapshot.json   as_of      > SNAPSHOT_MAX_DAYS  → stale（工作日刷新，留长周末余量）
      {key}-financials.json updated_at > FINANCIALS_MAX_DAYS → stale（每月 1 日刷新）
      {key}-news.json       as_of      > NEWS_MAX_DAYS       → stale（每天刷新，见下方常量注释）
      {key}-news.json       announcements_error == true      → 单独一类告警（见 check_news_announcements_error）
  - 每个 research_peers.json 注册代码：
      {code}-peers-market.json as_of    > PEERS_MAX_DAYS     → stale
  文件缺失 / 时间字段无法解析也算 stale。

退出码：0 = 正常（含已发告警邮件）；1 = 脚本自身错误（触发 cron-wrapper 告警）。
"""
import json
import pathlib
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

REPO_DIR = pathlib.Path(__file__).resolve().parents[1]
STOCKS_FILE = REPO_DIR / "config" / "research_stocks.json"
PEERS_FILE = REPO_DIR / "config" / "research_peers.json"
DOCS_DATA_DIR = pathlib.Path.home() / "docs-site" / "data"
ENV_FILE = pathlib.Path.home() / ".stock-monitor.env"
BJT = timezone(timedelta(hours=8))

SNAPSHOT_MAX_DAYS = 4     # 工作日刷新；4 天容忍长周末，持续失败次日即被抓
FINANCIALS_MAX_DAYS = 40  # 每月 1 日刷新
PEERS_MAX_DAYS = 4
CONSENSUS_MAX_DAYS = 40   # 每月 1 日刷新，与 financials 同频同阈值
# research-news cron 每天跑（不是工作日，不需要像 SNAPSHOT_MAX_DAYS 那样容长周末）；
# 3 天 = 容一次漏跑，还留一天余量再报警，不会跑丢一次就当天炸告警。
NEWS_MAX_DAYS = 3


def _load_env() -> dict:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"')
    return env


def _send_email(env: dict, subject: str, html_body: str) -> None:
    smtp_user = env["SMTP_USER"]
    smtp_pass = env["SMTP_PASS"]
    smtp_server = env.get("SMTP_SERVER", "smtp.163.com")
    smtp_port = int(env.get("SMTP_PORT", "465"))
    to_addr = env.get("MAIL_TO", smtp_user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as s:
        s.login(smtp_user, smtp_pass)
        s.sendmail(smtp_user, [to_addr], msg.as_string())
    print(f"  告警邮件已发送 → {to_addr}", flush=True)


def age_days(date_str, today: date):
    """date_str: 'YYYY-MM-DD' 或带时间的 ISO；返回距今天数，无法解析返回 None。"""
    if not date_str:
        return None
    s = str(date_str)[:10]
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    return (today - d).days


def check_file(path: pathlib.Path, date_field: str, max_days: int, today: date):
    """返回 stale 记录 dict 或 None（新鲜）。文件缺失/字段缺失/超阈值均算 stale。"""
    label = path.name
    if not path.exists():
        return {"file": label, "issue": "文件缺失"}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"file": label, "issue": f"无法解析 JSON: {e}"}
    val = d.get(date_field)
    age = age_days(val, today)
    if age is None:
        return {"file": label, "issue": f"{date_field} 缺失/无法解析 ({val})"}
    if age > max_days:
        return {"file": label, "issue": f"{date_field}={str(val)[:10]} 已 {age} 天（阈值 {max_days}）"}
    return None


def check_consensus_source(stocks: list, data_dir: pathlib.Path, today: date) -> list:
    """估值分母是否缺失。

    ★这是本脚本原本抓不到的一类失败。**2026-08-09 之前**：抓取失败会回落到
    注册表冻结值，页面不空不报错，平静地显示一个用登记日旧预期算出的 PE/PS
    （旭创注册表写 480 亿、实际 548 亿，差 14%）——静默的错数字比明显的空值危险得多。
    **之后**：用户拍板删掉兜底，失败即 `consensus_source="missing"`，页面不渲染倍数。
    危害从「显示错数」变成「少显示一块」，但仍必须告警——
    否则页面会安静地缺一块，同样没人知道。

    文件缺失不在这里报 —— 已由 `check_file` 报「文件缺失」，不重复。
    """
    out = []
    for s in stocks:
        key = s.get("snapshot_key") or s.get("symbol")
        path = data_dir / f"{key}-snapshot.json"
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue      # JSON 坏了同样由 check_file 报
        src = d.get("consensus_source")
        if src == "auto":
            continue
        detail = "字段缺失" if src is None else f"consensus_source={src}"
        out.append({
            "file": path.name,
            "issue": (f"估值分母缺失（{detail}）——注册表兜底已于 2026-08-09 删除，"
                      f"页面将不显示 PE/PS，请检查 research-consensus cron"),
        })
    return out


def check_news_announcements_error(stocks: list, data_dir: pathlib.Path) -> list:
    """`{key}-news.json` 的 `announcements_error=true` → 本次公告抓取失败，公告层未知。

    ★这是与 as_of 陈旧检查（check_file）成因不同的另一类失败，故意分开报、
    分开写告警文案，不合并成一条——否则以后自己都分不清是哪种：

    - `announcements_error=true` 时，若磁盘上已有旧文件，`write_and_deploy()`
      会**拒绝覆盖**（update_research_news.py 的刻意保护，不是 bug），`as_of`
      因此停在旧值上不动 → 这只股票很可能**同时**触发 as_of 陈旧检查和这里，
      这是预期的，不是重复告警。
    - 只触发 as_of 陈旧、不触发这里 = 采集大概率根本没跑（cron 没执行 /
      整个脚本挂了），news.json 连尝试写一份 error 标记的新文件都没发生。
    - 只触发这里、不触发 as_of = 首次采集就失败（没有旧文件可保护），写了一份
      带 error 标记的新文件，`as_of` 是今天，as_of 检查逮不到，只能靠这里报。

    文件缺失 / JSON 解析失败已由 check_file 报，这里不重复。
    """
    out = []
    for s in stocks:
        key = s.get("snapshot_key") or s.get("symbol")
        path = data_dir / f"{key}-news.json"
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue      # JSON 坏了同样由 check_file 报，不重复
        if d.get("announcements_error"):
            out.append({
                "file": path.name,
                "issue": ("公告本次抓取失败（announcements_error=true）——公告层未知，"
                          "不是「无公告」。若同一只股票的 as_of 也报陈旧，说明旧文件被"
                          "保护未覆盖（采集持续失败）；若 as_of 仍新鲜，说明是首次采集"
                          "失败。两种都请检查 research-news cron"),
            })
    return out


MISSED_RUNS_TOLERANCE = 1   # 容忍一次跳过（单次网络抖动），第二次才算异常


def _consensus_fetched_date(path: pathlib.Path):
    if not path.exists():
        return None
    try:
        val = json.loads(path.read_text(encoding="utf-8")).get("fetched_at")
        return date.fromisoformat(str(val)[:10])
    except Exception:  # noqa: BLE001
        return None


def check_consensus_cadence(key: str, data_dir: pathlib.Path, today: date):
    """按「本应触发几次」判断 consensus 是否停摆，而不是固定天数阈值。

    ★固定天数在这里是双向失效的：`CONSENSUS_MAX_DAYS=40` 按月频设，
    而 4/5/8/9 月已改为每周一抓——40 天恰好容得下**连续 5 次周失败**，
    加密的意义被抵消；若单纯改成 10 天，又会在加密月初误报
    （4 月第一个周一可能是 4-06，此前最近一次抓取是 3-01，距今 31 天却完全正常）。

    改用 cron 规则推算，语义不依赖月份边界，将来调频率也不用重调阈值。
    """
    import importlib.util
    import sys as _sys

    fetched = _consensus_fetched_date(data_dir / f"{key}-consensus.json")
    if fetched is None:
        return None      # 文件缺失/损坏已由 check_file 报，不重复

    mod = _sys.modules.get("update_research_consensus")
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            "update_research_consensus",
            pathlib.Path(__file__).resolve().parent / "update_research_consensus.py")
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["update_research_consensus"] = mod
        spec.loader.exec_module(mod)

    missed = mod.expected_runs_between(fetched, today)
    if missed <= MISSED_RUNS_TOLERANCE:
        return None
    return {
        "file": f"{key}-consensus.json",
        "issue": (f"自 {fetched} 以来按 cron 规则应跑 {missed} 次却一次都没更新 —— "
                  f"估值分母可能已停摆，请检查 research-consensus cron"),
    }


def check_history_keeps_up(key: str, data_dir: pathlib.Path):
    """修正历史是否跟得上 consensus。

    ★`write_and_deploy()` 把 history 写入包在 try/except 里（历史是加分项，
    不该拖垮当期数据），失败只打 WARN、脚本仍 exit 0 → cron-wrapper 不告警。
    最坏之处是**无法区分**：`revision_momentum` 会一直返回 `insufficient`，
    而这个状态和「刚开始积累、点数还不够」长得一模一样，
    几个月后想看修正方向才发现一个点都没攒下。
    """
    fetched = _consensus_fetched_date(data_dir / f"{key}-consensus.json")
    if fetched is None:
        return None

    hp = data_dir / f"{key}-consensus-history.jsonl"
    if not hp.exists():
        return {"file": hp.name,
                "issue": f"修正历史文件缺失，而 consensus 已刷到 {fetched} —— "
                         f"revision momentum 将永远算不出"}

    latest = None
    for line in hp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line).get("as_of")
        except json.JSONDecodeError:
            continue
        if d and (latest is None or d > latest):
            latest = d
    if latest and date.fromisoformat(latest) >= fetched:
        return None
    return {"file": hp.name,
            "issue": f"修正历史停在 {latest or '空'}，而 consensus 已刷到 {fetched} —— "
                     f"历史写入疑似静默失败，momentum 会一直显示「数据不足」"}


def collect_stale(today: date) -> list:
    stale = []
    stocks = json.loads(STOCKS_FILE.read_text())
    for s in stocks:
        key = s.get("snapshot_key") or s.get("symbol")
        for r in (
            check_file(DOCS_DATA_DIR / f"{key}-snapshot.json", "as_of", SNAPSHOT_MAX_DAYS, today),
            check_file(DOCS_DATA_DIR / f"{key}-financials.json", "updated_at", FINANCIALS_MAX_DAYS, today),
            # news 之前完全没被监控：cron 行被误删/整脚本挂了都是静默失败，
            # 页面 as_of 一天天变旧而无人知晓，页面上也不会有任何提示。
            check_file(DOCS_DATA_DIR / f"{key}-news.json", "as_of", NEWS_MAX_DAYS, today),
            # consensus 是估值分母的来源却一直没被监控：整月 cron 失败时 snapshot 照常
            # 每日刷新（分子是新的），分母停在上个月，三类检查全部显示「新鲜」。
            check_file(DOCS_DATA_DIR / f"{key}-consensus.json", "fetched_at", CONSENSUS_MAX_DAYS, today),
            # 按 cron 规则推算的停摆检查（比固定天数更贴合变动的抓取频率）
            check_consensus_cadence(key, DOCS_DATA_DIR, today),
            # 修正历史静默失败——不查的话 momentum 永远「数据不足」且无人知晓
            check_history_keeps_up(key, DOCS_DATA_DIR),
        ):
            if r:
                stale.append(r)
    stale.extend(check_consensus_source(stocks, DOCS_DATA_DIR, today))
    stale.extend(check_news_announcements_error(stocks, DOCS_DATA_DIR))
    if PEERS_FILE.exists():
        peers_cfg = json.loads(PEERS_FILE.read_text())
        for code in peers_cfg:
            if code.startswith("_"):
                continue
            r = check_file(DOCS_DATA_DIR / f"{code}-peers-market.json", "as_of", PEERS_MAX_DAYS, today)
            if r:
                stale.append(r)
    return stale


def build_html(stale: list, today: date) -> str:
    rows = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{r['file']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{r['issue']}</td></tr>"
        for r in stale
    )
    n_fallback = sum(1 for r in stale if "兜底" in r["issue"])
    n_stale = len(stale) - n_fallback
    parts = []
    if n_stale:
        parts.append(f"{n_stale} 个数据文件超过新鲜度阈值")
    if n_fallback:
        parts.append(f"<b>{n_fallback} 只标的的估值分母已回落到注册表兜底值</b>")
    return (
        "<html><body style='font-family:sans-serif'>"
        f"<h3>⚠️ 研报数据告警（{today} BJT）</h3>"
        f"<p>{'；'.join(parts)}。研报可能在<b>静默显示错误数字</b>——请检查对应 cron"
        "（research-snapshots / research-consensus / research-peers-market / research-financials）。</p>"
        "<table style='border-collapse:collapse;font-size:13px'>"
        "<tr><th style='text-align:left;padding:6px 10px;border-bottom:2px solid #333'>文件</th>"
        "<th style='text-align:left;padding:6px 10px;border-bottom:2px solid #333'>问题</th></tr>"
        f"{rows}</table>"
        f"<p style='color:#888;font-size:12px'>阈值：snapshot/peers-market &gt; {SNAPSHOT_MAX_DAYS} 天，"
        f"news &gt; {NEWS_MAX_DAYS} 天，"
        f"financials &gt; {FINANCIALS_MAX_DAYS} 天。由 research-data-health cron 每日检查。</p>"
        "</body></html>"
    )


def main() -> int:
    today = datetime.now(BJT).date()
    stale = collect_stale(today)

    # 研报正文过时告警已于 2026-08-10 停用（用户：「现在意义不大」）。
    # 背景：它是为了发现「正文的估值判断已被现价推翻」而建的，而当天做的两件事
    # 让它失去了对象——narrative 周更 agent 删除后正文不再被自动改写；
    # 旭创那类真正过时的结论段直接删掉了（没有会过时的结论，就没有过时可查）。
    # 其余各页的估值结论均带显式日期，读者看日期即知新旧。
    if not stale:
        print(f"OK — 所有研报数据新鲜（检查于 {today} BJT）")
        return 0

    print(f"STALE — {len(stale)} 个数据文件陈旧:")
    for r in stale:
        print(f"  {r['file']}: {r['issue']}")
    env = _load_env()
    try:
        _send_email(env, f"[研报数据告警] {len(stale)} 个文件陈旧（{today}）",
                    build_html(stale, today))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR 发送告警邮件失败: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
