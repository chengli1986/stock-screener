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

import argparse
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
# 判定所需的快照与一致预期都在 docs-site/data（由 snapshots / consensus 两个 cron 写出）
DATA_DIR = pathlib.Path(os.path.expanduser("~/docs-site")) / "data"
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


# ── 触发条件（2026-08-05 重写）────────────────────────────────────────────────
#
# 旧实现每只股票写死一个价位，两个月只触发过一次；**全池普跌 30–50% 时一条都没响**——
# 那正是最该提醒的时刻。根因有二：价位是用旧盈利预测算的（分母一变就错），
# 且阈值定得过深（多数标的还要跌 20–76%）。
#
# 新逻辑捕捉本轮回撤里实证发现的信号：**杀估值而不杀盈利**。
# 旭创跌 33% 而 2027E 预期被上调 14%（错位，值得看）；
# 源杰跌 35% 但 2027E 营收被下调 24%（基本面恶化，不该买）。

COOLDOWN_DAYS = 7
DRAWDOWN_MIN_PCT = 25.0      # ①a 距 52 周高回撤门槛
PEAK_MAX_AGE_DAYS = 120      # ①b 高点时效：近期急跌 vs 长期阴跌（用户提出）
PEG_MAX = 1.0                # ③ 增速须撑得住估值
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _horizon import horizon_years  # noqa: E402

# 预测视界：当前年+次年，按日历滚动（用户判断「28 年太遥远，一年半够了」）。
# 曾在四处各写死一份 ("2026E","2027E")，2027 年一到全会错。
HORIZON = horizon_years()


def _denominator(consensus: dict, year: str):
    """估值分母：优先截尾均值（preferred_value），回落 estimates 均值。"""
    b = (consensus.get("broker_stats") or {}).get(year) or {}
    v = b.get("preferred_value")
    if v:
        return v
    return ((consensus.get("estimates") or {}).get(year) or {}).get("profit_yuan")


def _credibility_reasons(consensus: dict) -> list[str]:
    """估值可信度门禁。数据不可信时一律不提醒——基于坏分母的买点是徒劳的。"""
    out = []
    xc = consensus.get("cross_check") or {}
    ra = consensus.get("range_agreement") or {}
    bs = consensus.get("broker_stats") or {}
    for y in HORIZON:
        for item in (xc.get(y) or {}).values():
            if item.get("verdict") == "DIVERGENT":
                out.append(f"{y} 两源背离")
            elif item.get("verdict") == "UNVERIFIED":
                out.append(f"{y} 未验证（无跨源）")
        if (ra.get(y) or {}).get("verdict") == "SAMPLE_DIVERGENT":
            out.append(f"{y} 两源样本构成分歧")
        if (bs.get(y) or {}).get("insufficient_samples"):
            out.append(f"{y} 机构覆盖不足")
    return sorted(set(out))


def evaluate(snapshot: dict, consensus: dict) -> tuple[bool, list[str]]:
    """判定是否触发买点。返回 `(是否触发, 未满足/需注意的说明列表)`。

    **一次列全所有不满足项**，而不是遇到第一个就返回——便于判断离触发还差多少。
    """
    reasons: list[str] = []
    tech = snapshot.get("technical") or {}
    price = snapshot.get("price_yuan")
    high = tech.get("week52_high")
    age = tech.get("week52_high_age_days")

    # ①a 回撤幅度
    dd = (price / high - 1) * 100 if (price and high) else None
    if dd is None:
        reasons.append("无法计算回撤（缺价格或 52 周高）")
    elif dd > -DRAWDOWN_MIN_PCT:
        reasons.append(f"回撤仅 {dd:.0f}%，未达 -{DRAWDOWN_MIN_PCT:.0f}%")

    # ①b 高点时效
    if age is None:
        reasons.append("高点日期不可得，无法判断是近期急跌还是长期阴跌")
    elif age > PEAK_MAX_AGE_DAYS:
        reasons.append(f"高点已过 {age} 天（>{PEAK_MAX_AGE_DAYS}），属长期阴跌而非近期急跌")

    # 次新股：52 周窗口未满，回撤只是「上市以来」口径
    if tech.get("week52_is_full") is False:
        reasons.append("上市未满一年，回撤为上市以来口径，仅供参考")

    # ③ PEG
    p26, p27 = _denominator(consensus, "2026E"), _denominator(consensus, "2027E")
    cap = (snapshot.get("market_cap_yi") or 0) * 1e8
    peg = None
    if p26 and p27 and p26 > 0 and p27 > 0 and cap:
        growth = (p27 / p26 - 1) * 100
        if growth > 0:
            peg = (cap / p27) / growth
        else:
            reasons.append(f"2027E 预期较 2026E 无增长（{growth:.0f}%），PEG 不适用")
    else:
        reasons.append("PEG 不可计算（缺预测或市值）")
    if peg is not None and peg >= PEG_MAX:
        reasons.append(f"PEG {peg:.2f} ≥ {PEG_MAX}")

    # ④ 可信度门禁
    reasons.extend(_credibility_reasons(consensus))

    blocking = [r for r in reasons if "仅供参考" not in r]
    return (not blocking), reasons


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
def _build_alert_email(triggered: list[dict]) -> tuple[str, str]:
    """触发买点 → 邮件标题与正文。标的名与理由均 html.escape（注册表是可编辑输入）。"""
    import html as _h

    names = "、".join(t["name"] for t in triggered)
    subject = f"[买点提醒] {names} 满足回撤+估值条件（{date.today().isoformat()}）"

    rows = []
    for t_ in triggered:
        s, c = t_["snapshot"], t_["consensus"]
        tech = s.get("technical") or {}
        px, hi = s.get("price_yuan"), tech.get("week52_high")
        dd = (px / hi - 1) * 100 if (px and hi) else None
        p26, p27 = _denominator(c, "2026E"), _denominator(c, "2027E")
        cap = (s.get("market_cap_yi") or 0) * 1e8
        pe27 = cap / p27 if p27 else None
        g = (p27 / p26 - 1) * 100 if (p26 and p27) else None
        peg = pe27 / g if (pe27 and g and g > 0) else None
        notes = "；".join(_h.escape(n) for n in t_["notes"]) or "—"
        rows.append(
            "<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'><b>{_h.escape(t_['name'])}</b></td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right'>¥{px:.2f}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right;color:#c00'>{dd:.0f}%</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right'>{tech.get('week52_high_age_days')} 天</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right'>{pe27:.1f}x</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right'>{peg:.2f}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;font-size:12px;color:#666'>{notes}</td>"
            "</tr>"
        )

    return subject, (
        "<html><body style='font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;color:#222'>"
        f"<h2 style='margin:0 0 6px'>买点提醒 · {len(triggered)} 只</h2>"
        "<p style='margin:0 0 14px;color:#666'>触发条件：距 52 周高回撤 ≥"
        f"{DRAWDOWN_MIN_PCT:.0f}% · 高点距今 ≤{PEAK_MAX_AGE_DAYS} 天（近期急跌而非长期阴跌） · "
        f"PEG &lt; {PEG_MAX}（{HORIZON[-1]} 口径） · 通过估值可信度门禁</p>"
        "<table style='border-collapse:collapse;font-size:13px'>"
        "<tr style='background:#f5f5f5'>"
        "<th style='padding:6px 10px;text-align:left'>标的</th>"
        "<th style='padding:6px 10px;text-align:right'>现价</th>"
        "<th style='padding:6px 10px;text-align:right'>距高点</th>"
        "<th style='padding:6px 10px;text-align:right'>高点距今</th>"
        f"<th style='padding:6px 10px;text-align:right'>{HORIZON[-1]} PE</th>"
        "<th style='padding:6px 10px;text-align:right'>PEG</th>"
        "<th style='padding:6px 10px;text-align:left'>备注</th>"
        "</tr>" + "".join(rows) + "</table>"
        "<p style='margin:16px 0 0;color:#666;font-size:12px'>"
        "估值分母为截尾均值（剔除两端极值），已通过双源交叉复核与区间重叠度检查。"
        "本邮件由 <code>research_price_alert.py</code> 自动发出，不构成投资建议。</p>"
        "</body></html>"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="研究股票买点提醒（条件式，非固定价位）")
    ap.add_argument("--dry-run", action="store_true", help="只打印判定，不发邮件、不写状态")
    args = ap.parse_args()

    print(f"=== research_price_alert ({datetime.now(BJT):%Y-%m-%d %H:%M} BJT) ===", flush=True)
    print(f"条件：回撤≥{DRAWDOWN_MIN_PCT:.0f}% · 高点≤{PEAK_MAX_AGE_DAYS}天 · "
          f"PEG<{PEG_MAX} · 估值可信度门禁（视界 {'/'.join(HORIZON)}）\n", flush=True)

    env = _load_env()
    if not args.dry_run and (not env.get("SMTP_USER") or not env.get("SMTP_PASS")):
        print("ERROR: SMTP_USER / SMTP_PASS not found in .stock-monitor.env", file=sys.stderr)
        return 1
    if not CONFIG_FILE.exists():
        print(f"ERROR: config not found: {CONFIG_FILE}", file=sys.stderr)
        return 1

    stocks = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    state = _load_state() if not args.dry_run else {}
    today = date.today().isoformat()
    triggered: list[dict] = []

    for stock in stocks:
        key = stock["snapshot_key"]
        name = stock["name"]
        snap_f = DATA_DIR / f"{key}-snapshot.json"
        cons_f = DATA_DIR / f"{key}-consensus.json"
        if not snap_f.exists() or not cons_f.exists():
            print(f"  [{key}] {name}: 缺快照或一致预期，跳过", flush=True)
            continue
        snapshot = json.loads(snap_f.read_text(encoding="utf-8"))
        consensus = json.loads(cons_f.read_text(encoding="utf-8"))

        ok, reasons = evaluate(snapshot, consensus)
        if ok:
            # 冷却：同一只 COOLDOWN_DAYS 内不重复提醒
            last = (state.get(key) or {}).get("last_fired")
            if last and (date.fromisoformat(today) - date.fromisoformat(last)).days < COOLDOWN_DAYS:
                print(f"  [{key}] {name}: 🔔 满足条件但在冷却期内（上次 {last}）", flush=True)
                continue
            triggered.append({"key": key, "name": name, "snapshot": snapshot,
                              "consensus": consensus, "notes": reasons})
            print(f"  [{key}] {name}: 🔔 触发买点", flush=True)
        else:
            print(f"  [{key}] {name}: — {'; '.join(reasons[:3])}", flush=True)

    if not triggered:
        print("\n本次无标的触发。", flush=True)
        return 0

    print(f"\n=== 触发 {len(triggered)} 只：{'、'.join(t['name'] for t in triggered)} ===", flush=True)
    if args.dry_run:
        print("（dry-run，未发邮件、未写状态）", flush=True)
        return 0

    subject, html = _build_alert_email(triggered)
    _send_email(env, subject, html)
    for t_ in triggered:
        state.setdefault(t_["key"], {})["last_fired"] = today
    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
