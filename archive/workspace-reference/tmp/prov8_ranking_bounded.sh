#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
[[ "$RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED" == true ]]
[[ "${EXECUTION_ENABLED:-false}" == false ]]
mkdir -p reports/phase_prov8
/usr/bin/time -v .venv/bin/kalshi-bot find-opportunities \
  --model-name crypto_v2 --limit 5 --output reports/phase_prov8/crypto_opportunities.md
/usr/bin/time -v .venv/bin/kalshi-bot find-opportunities \
  --model-name weather_v2 --limit 5 --output reports/phase_prov8/weather_opportunities.md
