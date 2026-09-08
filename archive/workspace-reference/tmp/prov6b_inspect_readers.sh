#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
ENV=/etc/kalshi-bot/kalshi-bot.env
set -a; . "$ENV"; set +a
printf 'TIMERS\n'
systemctl list-timers --all --no-pager | grep -E 'kalshi|multicategory' || true
printf 'MATCHING_PROCESSES\n'
for p in /proc/[0-9]*; do
  pid=${p##*/}
  cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null || true)
  case "$cmd" in
    *'kalshi-bot ui'*|*'phase3bb-r8-unified-paper-gate'*) printf '%s\t%s\n' "$pid" "$cmd";;
  esac
done
printf 'LOCKS\n'
.venv/bin/kalshi-bot db-locks || true
