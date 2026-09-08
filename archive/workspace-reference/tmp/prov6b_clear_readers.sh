#!/usr/bin/env bash
set -euo pipefail

cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a

systemctl stop kalshi-multicategory-refresh-scheduler.timer
systemctl stop kalshi-multicategory-refresh-scheduler.service || true

for pid in 177225 273502; do
  if [[ -r "/proc/$pid/cmdline" ]]; then
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline")
    case "$pid:$cmd" in
      177225:*'kalshi-bot ui'*) kill -TERM "$pid" ;;
      273502:*'phase3bb-r8-unified-paper-gate'*) kill -TERM "$pid" ;;
      *) echo "Refusing to signal changed PID $pid: $cmd" >&2; exit 1 ;;
    esac
  fi
done

for _ in $(seq 1 30); do
  remaining=0
  for pid in 177225 273502; do
    [[ -d "/proc/$pid" ]] && remaining=$((remaining + 1))
  done
  [[ "$remaining" -eq 0 ]] && break
  sleep 1
done

for pid in 177225 273502; do
  [[ ! -d "/proc/$pid" ]] || { echo "PID $pid did not exit" >&2; exit 1; }
done

.venv/bin/kalshi-bot db-writer-monitor --json
.venv/bin/kalshi-bot db-locks
