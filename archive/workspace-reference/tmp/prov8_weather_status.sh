#!/usr/bin/env bash
set -euo pipefail
date -u
pgrep -af 'kalshi-bot forecast --model weather_v2 --limit 20' || true
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
.venv/bin/kalshi-bot db-locks || true
sqlite3 /var/lib/kalshi-bot/kalshi_phase1.db \
  'SELECT model_name,COUNT(*) FROM runtime_provenance_events GROUP BY model_name;'
