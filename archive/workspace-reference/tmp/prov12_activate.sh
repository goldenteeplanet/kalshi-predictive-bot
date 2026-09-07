#!/usr/bin/env bash
set -euo pipefail
env_file=/etc/kalshi-bot/kalshi-bot.env
if grep -q '^PROV12_DECISION_TRACE_PREVIEW_ENABLED=' "$env_file"; then
  sed -i 's/^PROV12_DECISION_TRACE_PREVIEW_ENABLED=.*/PROV12_DECISION_TRACE_PREVIEW_ENABLED=true/' "$env_file"
else
  printf '\nPROV12_DECISION_TRACE_PREVIEW_ENABLED=true\n' >>"$env_file"
fi
if ! grep -q '^PROV12_PROVENANCE_STALE_AFTER_MINUTES=' "$env_file"; then
  printf 'PROV12_PROVENANCE_STALE_AFTER_MINUTES=60\n' >>"$env_file"
fi
grep -E '^(PROV11_DASHBOARD_PREVIEW_ENABLED|PROV12_DECISION_TRACE_PREVIEW_ENABLED|PROV12_PROVENANCE_STALE_AFTER_MINUTES|EXECUTION_ENABLED|PAPER_TRADING_ENABLED|RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED)=' "$env_file" || true
systemctl restart kalshi-ui.service
for attempt in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:8080/api/provenance/drift-alerts \
    --output /tmp/prov12_alerts.json; then
    break
  fi
  sleep 1
done
test -s /tmp/prov12_alerts.json
ticker=$(/opt/kalshi-predictive-bot/.venv/bin/python -c \
  'import json; p=json.load(open("/tmp/prov12_alerts.json")); print(p["rows"][0]["ticker"])')
curl --fail --silent --show-error "http://127.0.0.1:8080/api/provenance/traces/$ticker" \
  --output /tmp/prov12_trace.json
curl --fail --silent --show-error "http://127.0.0.1:8080/system/provenance/$ticker" \
  --output /tmp/prov12_trace.html
curl --fail --silent --show-error http://127.0.0.1:8080/system/provenance \
  --output /tmp/prov12_dashboard.html
grep -q 'Decision Trace' /tmp/prov12_trace.html
grep -q 'Per-Market Drift Preview' /tmp/prov12_dashboard.html
mkdir -p /opt/kalshi-predictive-bot/reports/phase_prov12
install -o kalshi -g kalshi -m 0644 /tmp/prov12_alerts.json \
  /opt/kalshi-predictive-bot/reports/phase_prov12/prov12_drift_alerts.json
install -o kalshi -g kalshi -m 0644 /tmp/prov12_trace.json \
  /opt/kalshi-predictive-bot/reports/phase_prov12/prov12_sample_trace.json
echo PROV12_UI_SMOKE=PASS
echo PROV12_SAMPLE_TICKER=$ticker
systemctl show kalshi-ui.service -p MainPID -p ActiveState -p SubState -p MemoryCurrent
