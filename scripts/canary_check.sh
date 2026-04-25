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
