#!/usr/bin/env bash
set -euo pipefail
env_file=/etc/kalshi-bot/kalshi-bot.env
if grep -q '^PROV11_DASHBOARD_PREVIEW_ENABLED=' "$env_file"; then
  sed -i 's/^PROV11_DASHBOARD_PREVIEW_ENABLED=.*/PROV11_DASHBOARD_PREVIEW_ENABLED=true/' "$env_file"
else
  printf '\nPROV11_DASHBOARD_PREVIEW_ENABLED=true\n' >>"$env_file"
fi
grep -E '^(PROV11_DASHBOARD_PREVIEW_ENABLED|EXECUTION_ENABLED|PAPER_TRADING_ENABLED|RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED)=' "$env_file" || true
systemctl restart kalshi-ui.service
systemctl is-active kalshi-ui.service
systemctl show kalshi-ui.service -p MainPID -p ActiveState -p SubState -p MemoryCurrent
for attempt in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:8080/api/provenance/diagnostics \
    --output /tmp/prov11_api.json; then
    break
  fi
  sleep 1
done
test -s /tmp/prov11_api.json
curl --fail --silent --show-error http://127.0.0.1:8080/system/provenance \
  --output /tmp/prov11_page.html
curl --fail --silent --show-error http://127.0.0.1:8080/system \
  --output /tmp/prov11_system.html
grep -q 'Runtime Provenance Diagnostics' /tmp/prov11_page.html
grep -q 'Runtime Provenance' /tmp/prov11_system.html
/opt/kalshi-predictive-bot/.venv/bin/python -m json.tool /tmp/prov11_api.json >/dev/null
echo PROV11_UI_SMOKE=PASS
