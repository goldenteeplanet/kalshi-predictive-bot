#!/usr/bin/env bash
set -euo pipefail
pid=284094
if [[ -r "/proc/$pid/cmdline" ]]; then
  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline")
  case "$cmd" in
    'sqlite3 /var/lib/kalshi-bot/kalshi_phase1.db PRAGMA quick_check(1); '*)
      kill -TERM "$pid"
      ;;
    *)
      echo "Refusing to terminate unexpected PID $pid: $cmd" >&2
      exit 1
      ;;
  esac
fi
for _ in $(seq 1 10); do
  [[ ! -d "/proc/$pid" ]] && break
  sleep 1
done
[[ ! -d "/proc/$pid" ]]

cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
.venv/bin/alembic current
.venv/bin/kalshi-bot db-locks || true
