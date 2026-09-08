#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
[[ "$RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED" == true ]]
[[ "${EXECUTION_ENABLED:-false}" == false ]]
systemctl reset-failed kalshi-multicategory-refresh-scheduler.service || true
systemctl start kalshi-ui.service
systemctl start kalshi-r5-watcher.service
systemctl start kalshi-multicategory-refresh-scheduler.timer
printf 'ui=%s\nr5=%s\nscheduler_timer=%s\n' \
  "$(systemctl is-active kalshi-ui.service)" \
  "$(systemctl is-active kalshi-r5-watcher.service)" \
  "$(systemctl is-active kalshi-multicategory-refresh-scheduler.timer)"
