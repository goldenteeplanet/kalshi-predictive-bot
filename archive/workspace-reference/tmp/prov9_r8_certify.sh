#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/kalshi-bot/kalshi-bot.env
set +a
cd /opt/kalshi-predictive-bot
rm -rf /tmp/prov9_r8_final
/usr/bin/time -v ./.venv/bin/kalshi-bot phase3bb-r8-unified-paper-gate \
  --output-dir /tmp/prov9_r8_final \
  --reports-dir reports \
  --limit-per-category 50
