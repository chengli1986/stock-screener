#!/bin/bash
# Daily canary for stock-screener: runs phase0_spike.py --limit 15 to verify
# the 3 upstream data APIs (akshare CSI / Longbridge OHLCV / East Money push2)
# are still healthy, without overwriting the full-run baseline.
#
# Output goes to artifacts/canary-latest/ (separate dir via PHASE0_ARTIFACTS_SUBDIR).
# Designed to be invoked through ~/cron-wrapper.sh, which handles timeout, lock,
# JSONL logging to ~/logs/ops-status.jsonl, and email alerting on non-zero exit.
#
# Pass criteria (15 fixed dry-run samples = 10 A + 5 HK):
#   - universe.total == 15
#   - ohlcv ok >= 14   (allow at most 1 transient failure)
#   - fundamentals ok >= 14
# Anything weaker → exit 1 → cron-wrapper sends an alert email.

set -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$HOME/stock-env/bin/python3"
SPIKE="$REPO_DIR/scripts/phase0_spike.py"

export PHASE0_ARTIFACTS_SUBDIR="canary-latest"
REPORT="$REPO_DIR/artifacts/canary-latest/report.json"

# --workers 1 avoids a Longbridge CLI OAuth race: when multiple workers hit a
# token-refresh window simultaneously, they all try to bind callback port 6035
# and most fail. Canary cost stays under 90s with serial OHLCV.
# --force ignores resume — a stale ok row from yesterday must not mask today's failure.
if ! "$PYTHON" "$SPIKE" --limit 15 --workers 1 --force; then
    echo "[canary] phase0_spike.py exited non-zero" >&2
    exit 1
fi

if [[ ! -f "$REPORT" ]]; then
    echo "[canary] $REPORT not produced" >&2
    exit 1
fi

export REPORT
python3 << 'PYEOF'
import json, os, sys

report = json.load(open(os.environ["REPORT"], encoding="utf-8"))
u_total = report["universe"]["total"]
ok_ohlcv = report["ohlcv"]["a"]["ok"] + report["ohlcv"]["hk"]["ok"]
ok_fund  = report["fundamentals"]["a"]["ok"] + report["fundamentals"]["hk"]["ok"]

issues = []
if u_total != 15:
    issues.append(f"universe={u_total} (expected 15)")
if ok_ohlcv < 14:
    issues.append(f"ohlcv ok={ok_ohlcv}/15 (threshold >=14)")
if ok_fund < 14:
    issues.append(f"fundamentals ok={ok_fund}/15 (threshold >=14)")

if issues:
    print("[canary] FAIL: " + "; ".join(issues), file=sys.stderr)
    sys.exit(1)

print(f"[canary] OK: universe={u_total} ohlcv={ok_ohlcv}/15 fundamentals={ok_fund}/15")
PYEOF

# ── 新 API 探针：腾讯财经 K 线 + THS 质量指标 ─────────────────────────────────
# 固定 3 只 fixture stocks（主板 + 科创板 + A 股代表），验证两条新数据链。
# 通过标准：
#   - 腾讯财经主板(000333)、科创板(688256) 各返回 >=200 条日线
#   - THS(600519) 年报三字段(ROE/负债率/CFO)均非空
echo "[canary] probing Tencent Finance K-line + THS quality APIs..."
if ! "$PYTHON" << 'PYEOF'
import sys, requests, time
from datetime import datetime, timezone, timedelta
import akshare as ak

BJT = timezone(timedelta(hours=8))
now = datetime.now(BJT)
start_s = (now - timedelta(days=366)).strftime("%Y-%m-%d")
end_s   = now.strftime("%Y-%m-%d")

issues = []

# 1. 腾讯财经 K 线 — 主板 (sz) + 科创板 (sh, day fallback)
for sym, mkt, label in [("000333", "sz", "main-board"), ("688256", "sh", "STAR-market")]:
    code = f"{mkt}{sym}"
    try:
        r = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{code},day,{start_s},{end_s},300,qfq"},
            headers={"Referer": "https://gu.qq.com/"},
            timeout=10,
        )
        d = r.json()
        stock_data = d.get("data", {}).get(code, {})
        rows = stock_data.get("qfqday") or stock_data.get("day", [])
        if len(rows) < 200:
            issues.append(f"tencent {label}({sym}): {len(rows)} rows (expected >=200)")
        else:
            print(f"  tencent {label}({sym}): {len(rows)} rows OK")
    except Exception as e:
        issues.append(f"tencent {label}({sym}): {e}")
    time.sleep(0.3)

# 2. THS 质量指标 — 贵州茅台(600519)，验证年报三字段
try:
    df = ak.stock_financial_abstract_ths(symbol="600519", indicator="按报告期")
    df = df.sort_values("报告期", ascending=False)
    annual = df[df["报告期"].str.endswith("-12-31")]
    if annual.empty:
        issues.append("ths(600519): no annual report row found")
    else:
        row = annual.iloc[0]
        roe  = row.get("净资产收益率")
        debt = row.get("资产负债率")
        ocf  = row.get("每股经营现金流")
        missing = [k for k, v in [("ROE", roe), ("debt_ratio", debt), ("CFO", ocf)]
                   if v is False or v is None]
        if missing:
            issues.append(f"ths(600519) missing fields: {missing}")
        else:
            print(f"  ths(600519): ROE={roe} debt={debt} OCF={ocf} OK")
except Exception as e:
    issues.append(f"ths(600519): {e}")

if issues:
    for msg in issues:
        print(f"[canary] FAIL: {msg}", file=sys.stderr)
    sys.exit(1)

print("[canary] OK: tencent-kline + ths-quality probes passed")
PYEOF
then
    echo "[canary] new API probe failed" >&2
    exit 1
fi
