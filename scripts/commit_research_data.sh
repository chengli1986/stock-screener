#!/usr/bin/env bash
# commit_research_data.sh — commit + push research data files that a research-* cron
# just refreshed in the docs-site repo. Invoked by the research cron lines after their
# Python data-writer succeeds (chained with &&, so a failed refresh is never committed).
# Exception — research-news (2026-10-17): it also commits on exit 1, because that writer
# guards per stock (a failed fetch keeps the existing file), so exit 1 means "some stocks
# failed", not "bad data written"; with && one flaky stock blocked every other stock's
# commit. Signal exits (rc > 1, e.g. timeout kill) still skip the commit.
#
# Multi-session safe: stages ONLY the pathspec(s) passed in (never `git add -A` or a
# wholesale `git add data/`), so it won't sweep up a concurrent session's WIP edits to
# other data files, and commits ONLY the files staged under those pathspecs: a bare
# `git commit` would also commit whatever another session had staged in ~/docs-site
# (found by the 2026-11-21 docs-site writers audit). The file list, not the globs, is
# handed to `git commit`, so a pathspec that matches nothing cannot fail the commit.
# Push is best-effort with a single rebase-retry; a persistent push failure leaves a local commit for the next run/session and never fails the cron.
#
# Usage: commit_research_data.sh <pathspec> [<pathspec> ...]
#   e.g. commit_research_data.sh 'data/*-snapshot.json'
set -eo pipefail

REPO="$HOME/docs-site"
[ -d "$REPO/.git" ] || { echo "[commit-research-data] no git repo at $REPO, skip"; exit 0; }
cd "$REPO"

[ "$#" -ge 1 ] || { echo "[commit-research-data] no pathspec given, skip"; exit 0; }

# Only plain positive globs are supported (all 7 research-* crons pass those). A magic
# pathspec (":(exclude)...", ":(literal)...") would change meaning once staged one at a
# time below, so it is refused before the index is touched.
for spec in "$@"; do
  case "$spec" in
    :*) echo "[commit-research-data] ERROR: magic pathspec not supported: [$spec]"; exit 1 ;;
  esac
done

# Stage the pathspecs one at a time: given several, `git add` stages NOTHING if any one
# matches no file. A pathspec that matches no file (tracked, deleted-but-tracked or
# untracked) is skipped and said so; any other git failure (e.g. index.lock) stops the
# run before anything is committed.
for spec in "$@"; do
  if ! matches="$(git ls-files --cached --others --exclude-standard -- "$spec")"; then
    echo "[commit-research-data] ERROR: git ls-files failed for [$spec]"; exit 1
  fi
  if [ -z "$matches" ]; then
    echo "[commit-research-data] no file matches [$spec], skipped"
    continue
  fi
  if ! git add -- "$spec"; then
    echo "[commit-research-data] ERROR: git add failed for [$spec]"; exit 1
  fi
done

# Commit ONLY the files staged under our pathspecs -- not the whole index, which may
# hold another session's staged work. --no-renames lists a rename's deleted old path
# as well as the new one; -z keeps every byte of a file name; --literal-pathspecs makes
# git take each listed name as a name, not a pattern.
# Known limit (left to the docs-autosync work): files under OUR globs that another
# session changed in the shared ~/docs-site checkout are still committed with ours;
# telling them apart needs a cron-only worktree and a list of files the refresh wrote.
STAGED_LIST="$(mktemp)"
trap 'rm -f "$STAGED_LIST"' EXIT
if ! git diff --cached --name-only --no-renames -z -- "$@" > "$STAGED_LIST"; then
  echo "[commit-research-data] ERROR: git diff failed for [$*]"; exit 1
fi
mapfile -d '' -t STAGED < "$STAGED_LIST"
if [ "${#STAGED[@]}" -eq 0 ]; then
  echo "[commit-research-data] no changes staged for [$*], nothing to commit"
  exit 0
fi

DATE="$(TZ=Asia/Shanghai date '+%Y-%m-%d')"
if ! git --literal-pathspecs commit -q -m "chore(data): research data refresh ${DATE} (auto via cron)" \
       -m "Auto-committed by commit_research_data.sh for pathspec(s): $*" -- "${STAGED[@]}"; then
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
