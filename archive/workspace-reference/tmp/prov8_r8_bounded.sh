#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
[[ "${EXECUTION_ENABLED:-false}" == false ]]
rm -rf /tmp/prov8_r8
exec /usr/bin/time -v .venv/bin/kalshi-bot phase3bb-r8-unified-paper-gate \
  --output-dir /tmp/prov8_r8 --reports-dir reports --limit-per-category 50
