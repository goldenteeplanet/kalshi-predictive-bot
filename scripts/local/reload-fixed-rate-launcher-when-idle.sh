#!/usr/bin/env bash
set -euo pipefail

readonly TMUX_TARGET="${KALSHI_TMUX_TARGET:-kalshi-refresh-loop:0}"
readonly LAUNCHER="${KALSHI_FIXED_RATE_LAUNCHER:-/home/james/kalshi-runtime-src/scripts/kalshi-fixed-rate-refresh.sh}"
readonly WRITER_LOCK="${KALSHI_WRITER_LOCK:-/home/james/kalshi-local-runtime/kalshi-writer.lock}"
readonly DEADLINE_EPOCH="$(( $(date +%s) + 840 ))"

while (( $(date +%s) < DEADLINE_EPOCH )); do
  pane_pid="$(tmux display-message -pt "$TMUX_TARGET" '#{pane_pid}')"
  # The fixed-rate cycle can overrun its interval and therefore never enter
  # sleep.  The shared writer lock is the authoritative safe boundary; any
  # unlocked quote staging/status step is read-only and may be restarted.
  if flock -n "$WRITER_LOCK" -c true; then
    tmux respawn-pane -k -t "$TMUX_TARGET" "$LAUNCHER"
    printf '%s reloaded %s\n' "$(date -Is)" "$LAUNCHER"
    exit 0
  fi
  sleep 5
done

printf '%s no safe idle boundary found\n' "$(date -Is)" >&2
exit 1
