#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
[[ "$RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED" == true ]]
[[ "${EXECUTION_ENABLED:-false}" == false ]]
model=${1:?model required}
exec /usr/bin/time -v .venv/bin/kalshi-bot find-opportunities \
  --model-name "$model" --limit 5 --output "/tmp/prov8_${model}_opportunities.md"
