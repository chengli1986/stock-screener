#!/usr/bin/env python3
"""
research_price_alert.py — 研究股票买点价格提醒

读取 config/research_stocks.json 中配置了 price_alerts 的股票，
与 docs-site/data/{key}-snapshot.json 中的最新收盘价比对，
价格触达买点阈值时发送 HTML 邮件提醒。

冷却机制：同一档位 cooldown_days 内不重复发邮件，
状态持久化在 artifacts/price-alert-state.json。

退出码：0 = 正常（含无触发）；1 = 脚本错误（触发 cron-wrapper 告警）。
"""

import json
import os
import pathlib
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── paths ──────────────────────────────────────────────────────────────────────
REPO_DIR = pathlib.Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_DIR / "config" / "research_stocks.json"
STATE_FILE = REPO_DIR / "artifacts" / "price-alert-state.json"
DOCS_DATA_DIR = pathlib.Path.home() / "docs-site" / "data"
ENV_FILE = pathlib.Path.home() / ".stock-monitor.env"

BJT = timezone(timedelta(hours=8))
SITE_BASE = "https://docs.sinostor.com.cn"


# ── env loader ─────────────────────────────────────────────────────────────────
def _load_env() -> dict[str, str]:
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


# ── state ──────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── email ──────────────────────────────────────────────────────────────────────
def _send_email(env: dict, subject: str, html_body: str) -> None:
    smtp_user = env["SMTP_USER"]
    smtp_pass = env["SMTP_PASS"]
    smtp_server = env.get("SMTP_SERVER", "smtp.163.com")
    smtp_port = int(env.get("SMTP_PORT", "465"))
    to_addr = env.get("MAIL_TO", smtp_user)

    msg = MIMEMultipart("alternative")
    msg["MIME-Version"] = "1.0"
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as s:
        s.login(smtp_user, smtp_pass)
        s.sendmail(smtp_user, [to_addr], msg.as_string())
    print(f"  邮件已发送 → {to_addr}  主题: {subject}", flush=True)


# ── html email builder ─────────────────────────────────────────────────────────
def _build_email_html(
    stock: dict,
    alert: dict,
    price: float,
    as_of: str,
    pe_2026e: float | None,
    triggered_alerts: list[dict],
) -> tuple[str, str]:
    """返回 (subject, html_body)。triggered_alerts 是本次所有触发档位列表。"""
    name = stock["name"]
    symbol = stock["symbol"]
    page_url = SITE_BASE + stock.get("page", "")

    # 用最高优先档位（阈值最低）作为主题行
    best = min(triggered_alerts, key=lambda a: a["threshold"])
    subject = f"[买点提醒] {name}({symbol}) 触达「{best['label']}」¥{price:.1f} ≤ ¥{best['threshold']}"

    rows = ""
    for a in triggered_alerts:
        rows += (
            f"<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #2d333b'>"
            f"<strong style='color:#f59e0b'>{a['label']}</strong></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #2d333b;text-align:right'>"
            f"≤ ¥{a['threshold']}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #2d333b;color:#8b949e'>"
            f"{a['pe_band']}</td>"
            f"</tr>"
        )

    pe_str = f"{pe_2026e:.1f}x" if pe_2026e else "—"
    now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#c9d1d9">
<div style="max-width:560px;margin:32px auto;background:#161b22;border:1px solid #30363d;border-radius:12px;overflow:hidden">

  <!-- header -->
  <div style="background:linear-gradient(135deg,#1f2937,#111827);padding:20px 24px;border-bottom:2px solid #f59e0b">
    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">买点价格提醒</div>
    <div style="font-size:20px;font-weight:700;color:#f59e0b">{name} <span style="color:#8b949e;font-size:14px;font-weight:400">({symbol}.SZ)</span></div>
  </div>

  <!-- current price -->
  <div style="padding:20px 24px;border-bottom:1px solid #30363d;display:flex;gap:32px;flex-wrap:wrap">
    <div>
      <div style="font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">当前收盘价</div>
      <div style="font-size:32px;font-weight:700;color:#f85149">¥{price:.2f}</div>
      <div style="font-size:11px;color:#8b949e;margin-top:2px">截至 {as_of}</div>
    </div>
    <div>
      <div style="font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">2026E PE（当前价）</div>
      <div style="font-size:22px;font-weight:700;color:#e6edf3">{pe_str}</div>
    </div>
  </div>

  <!-- triggered zones -->
  <div style="padding:16px 24px;border-bottom:1px solid #30363d">
    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">已触达买点档位</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="color:#8b949e;font-size:11px">
          <th style="text-align:left;padding:6px 12px;border-bottom:1px solid #30363d">档位</th>
          <th style="text-align:right;padding:6px 12px;border-bottom:1px solid #30363d">阈值</th>
          <th style="text-align:left;padding:6px 12px;border-bottom:1px solid #30363d">PE 区间</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <!-- thesis reminder -->
  <div style="padding:16px 24px;border-bottom:1px solid #30363d;font-size:12.5px;line-height:1.7;color:#8b949e">
    <strong style="color:#e6edf3">操作提示</strong>：买点触达 ≠ 立即买入。建议结合当日成交量、大盘情绪及最新基本面数据综合判断。
    推荐止损纪律：介入成本 <strong style="color:#e6edf3">−15%</strong>；分批建仓，控制单次仓位。
  </div>

  <!-- link -->
  <div style="padding:16px 24px;font-size:12px;color:#8b949e">
    完整研究报告：<a href="{page_url}" style="color:#58a6ff;text-decoration:none">{page_url}</a>
    <br><span style="font-size:11px">本提醒由系统自动生成 · {now_str}</span>
  </div>

</div>
<p style="text-align:center;font-size:10px;color:#484f58;margin:12px 0 32px">
  仅供个人研究参考，不构成投资建议
</p>
</body>
</html>"""

    return subject, html


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"=== research_price_alert ({datetime.now(BJT):%Y-%m-%d %H:%M} BJT) ===", flush=True)

    env = _load_env()
    if not env.get("SMTP_USER") or not env.get("SMTP_PASS"):
        print("ERROR: SMTP_USER / SMTP_PASS not found in .stock-monitor.env", file=sys.stderr)
        return 1

    if not CONFIG_FILE.exists():
        print(f"ERROR: config not found: {CONFIG_FILE}", file=sys.stderr)
        return 1

    stocks = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    state = _load_state()
    today = date.today().isoformat()
    any_error = False

    for stock in stocks:
        alerts_config: list[dict] = stock.get("price_alerts", [])
        if not alerts_config:
            continue

        key = stock["snapshot_key"]
        symbol = stock["symbol"]
        snap_file = DOCS_DATA_DIR / f"{key}-snapshot.json"

        if not snap_file.exists():
            print(f"  [{symbol}] SKIP — snapshot 不存在: {snap_file}", flush=True)
            continue

        try:
            snap = json.loads(snap_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [{symbol}] ERROR 读取 snapshot: {e}", file=sys.stderr)
            any_error = True
            continue

        price: float | None = snap.get("price_yuan")
        as_of: str = snap.get("as_of", "—")
        pe_2026e: float | None = (snap.get("pe_estimates") or {}).get("2026E")

        if price is None:
            print(f"  [{symbol}] SKIP — snapshot 无 price_yuan", flush=True)
            continue

        # 快照超过2天认为数据过旧（节假日连休最多1天），不发提醒
        try:
            snap_date = date.fromisoformat(as_of)
            if (date.today() - snap_date).days > 2:
                print(f"  [{symbol}] SKIP — snapshot 过旧 ({as_of})", flush=True)
                continue
        except ValueError:
            pass

        print(f"  [{symbol}] 当前价 ¥{price:.2f}，as_of={as_of}", flush=True)

        sym_state: dict = state.setdefault(symbol, {})
        triggered: list[dict] = []

        for alert in alerts_config:
            label = alert["label"]
            threshold = float(alert["threshold"])
            cooldown = int(alert.get("cooldown_days", 3))

            if price > threshold:
                print(f"    [{label}] 未触达（¥{price:.1f} > ¥{threshold}）", flush=True)
                continue

            # 冷却检查
            last_sent = sym_state.get(label)
            if last_sent:
                days_since = (date.today() - date.fromisoformat(last_sent)).days
                if days_since < cooldown:
                    print(
                        f"    [{label}] 触达但冷却中（上次 {last_sent}，距今 {days_since}d < {cooldown}d）",
                        flush=True,
                    )
                    continue

            print(f"    [{label}] ✓ 触达！¥{price:.1f} ≤ ¥{threshold} → 准备发邮件", flush=True)
            triggered.append(alert)

        if not triggered:
            continue

        # 构建并发送邮件
        try:
            subject, html = _build_email_html(
                stock, triggered[0], price, as_of, pe_2026e, triggered
            )
            _send_email(env, subject, html)

            # 更新所有已触发档位的状态
            for a in triggered:
                sym_state[a["label"]] = today
            state[symbol] = sym_state
            _save_state(state)

        except Exception as e:
            print(f"  [{symbol}] ERROR 发送邮件: {e}", file=sys.stderr)
            any_error = True

    print(f"\n=== done ===", flush=True)
    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())
