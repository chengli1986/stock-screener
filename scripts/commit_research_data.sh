#!/usr/bin/env bash
# commit_research_data.sh — commit + push research data files that a research-* cron
# just refreshed in the docs-site repo. Invoked by the research cron lines after their
# Python data-writer succeeds (chained with &&, so a failed refresh is never committed).
#
# Multi-session safe: stages ONLY the pathspec(s) passed in (never `git add -A` or a
# wholesale `git add data/`), so it won't sweep up a concurrent session's WIP edits to
# other data files. Push is best-effort with a single rebase-retry; a persistent push
# failure leaves a local commit for the next run/session and never fails the cron.
#
# Usage: commit_research_data.sh <pathspec> [<pathspec> ...]
#   e.g. commit_research_data.sh 'data/*-snapshot.json'
set -eo pipefail

REPO="$HOME/docs-site"
[ -d "$REPO/.git" ] || { echo "[commit-research-data] no git repo at $REPO, skip"; exit 0; }
cd "$REPO"

[ "$#" -ge 1 ] || { echo "[commit-research-data] no pathspec given, skip"; exit 0; }

# Stage only the requested pathspec globs (git expands them as pathspecs).
git add -- "$@" 2>/dev/null || true

if git diff --cached --quiet; then
  echo "[commit-research-data] no changes staged for [$*], nothing to commit"
  exit 0
fi

DATE="$(TZ=Asia/Shanghai date '+%Y-%m-%d')"
if ! git commit -q -m "chore(data): research data refresh ${DATE} (auto via cron)" \
       -m "Auto-committed by commit_research_data.sh for pathspec(s): $*"; then
  echo "[commit-research-data] git commit failed"
  exit 1
fi

# Push best-effort: on non-fast-forward (concurrent push), try one rebase + retry; if it
# still fails, abort any rebase and keep the local commit for the next run to carry.
if git push -q origin main 2>/dev/null; then
  echo "[commit-research-data] committed + pushed [$*]"
elif git pull --rebase --autostash -q origin main 2>/dev/null && git push -q origin main 2>/dev/null; then
  echo "[commit-research-data] committed + pushed after rebase [$*]"
else
  git rebase --abort 2>/dev/null || true
  echo "[commit-research-data] WARNING: git push failed, local commit kept for next run [$*]"
fi
exit 0
