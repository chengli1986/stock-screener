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
    """估值分母是否已回落到注册表兜底值。

    ★这是本脚本原本抓不到的一类失败：抓取失败时页面**不空、不报错**，而是平静地
    显示一个用登记日旧预期算出的 PE/PS（旭创注册表写 480 亿、实际 548 亿，差 14%）。
    `consensus_source` 字段记下了，但没人会去看。在「读研报下投资判断」的用法下，
    静默的错数字比明显的空值危险得多。

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
            "issue": (f"估值分母回落到注册表兜底值（{detail}）——该值自登记日起从未更新，"
                      f"页面正在显示可能过时的 PE/PS，请检查 research-consensus cron"),
        })
    return out


def collect_stale(today: date) -> list:
    stale = []
    stocks = json.loads(STOCKS_FILE.read_text())
    for s in stocks:
        key = s.get("snapshot_key") or s.get("symbol")
        for r in (
            check_file(DOCS_DATA_DIR / f"{key}-snapshot.json", "as_of", SNAPSHOT_MAX_DAYS, today),
            check_file(DOCS_DATA_DIR / f"{key}-financials.json", "updated_at", FINANCIALS_MAX_DAYS, today),
            # consensus 是估值分母的来源却一直没被监控：整月 cron 失败时 snapshot 照常
            # 每日刷新（分子是新的），分母停在上个月，三类检查全部显示「新鲜」。
            check_file(DOCS_DATA_DIR / f"{key}-consensus.json", "fetched_at", CONSENSUS_MAX_DAYS, today),
        ):
            if r:
                stale.append(r)
    stale.extend(check_consensus_source(stocks, DOCS_DATA_DIR, today))
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
        f"financials &gt; {FINANCIALS_MAX_DAYS} 天。由 research-data-health cron 每日检查。</p>"
        "</body></html>"
    )


def main() -> int:
    today = datetime.now(BJT).date()
    stale = collect_stale(today)

    if not stale:
        print(f"OK — 所有研报数据新鲜（检查于 {today} BJT）")
        return 0

    print(f"STALE — {len(stale)} 个数据文件陈旧:")
    for r in stale:
        print(f"  {r['file']}: {r['issue']}")

    env = _load_env()
    try:
        _send_email(env, f"[研报数据告警] {len(stale)} 个文件陈旧（{today}）", build_html(stale, today))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR 发送告警邮件失败: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
