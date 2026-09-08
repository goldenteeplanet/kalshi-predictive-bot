set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
J=$(.venv/bin/kalshi-bot db-writer-monitor --json)
printf '%s\n' "$J"
printf '%s\n' "$J" | grep -q '"writer_count": 0'
printf '%s\n' "$J" | grep -q '"safe_to_start_write": true'
OUT=$(mktemp -d /tmp/kalshi-opportunity-gap-audit.XXXXXX)
.venv/bin/kalshi-bot category-coverage-gap-audit \
  --gh1-manifest-path /var/lib/kalshi-bot-gh1/watch/actionable_tickers.json \
  --gh2-report-path /var/lib/kalshi-bot-gh2/reports/gh2_active_candidate_refresh.json \
  --output-dir "$OUT" \
  --freshness-minutes 15
.venv/bin/kalshi-bot db-writer-monitor --json > "$OUT/writer_after.json"
echo OUT=$OUT
