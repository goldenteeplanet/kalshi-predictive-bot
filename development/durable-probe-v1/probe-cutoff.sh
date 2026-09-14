#!/bin/sh
set -eu
deadline=$(date -u -d '2026-09-14 13:44:56 UTC' +%s)
remaining=$((deadline - $(date -u +%s)))
if test "$remaining" -gt 0; then sleep "$remaining"; fi
pid=$(systemctl show alpha-cloud-durable-probe-v1-20260914.service -p MainPID --value)
if test "$pid" != 0; then
  systemctl kill --kill-whom=all --signal=SIGKILL alpha-cloud-durable-probe-v1-20260914.service
  printf '%s\n' 'OWNED_PROBE_KILLED_AT_CAPTURE_SAFETY_CUTOFF'
else
  printf '%s\n' 'OWNED_PROBE_ALREADY_TERMINAL_NO_KILL'
fi
date -u +%FT%TZ
