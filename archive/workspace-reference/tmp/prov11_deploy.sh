#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/root/prov11_backup_${stamp}
mkdir -p "$backup"
cp src/kalshi_predictor/config.py "$backup/config.py"
cp src/kalshi_predictor/ui/routes.py "$backup/routes.py"
cp src/kalshi_predictor/ui/templates/system_health.html "$backup/system_health.html"
cp /etc/kalshi-bot/kalshi-bot.env "$backup/kalshi-bot.env"
[[ -f src/kalshi_predictor/provenance/diagnostics.py ]] && \
  cp src/kalshi_predictor/provenance/diagnostics.py "$backup/diagnostics.py" || true
[[ -f src/kalshi_predictor/ui/templates/provenance_diagnostics.html ]] && \
  cp src/kalshi_predictor/ui/templates/provenance_diagnostics.html "$backup/provenance_diagnostics.html" || true
install -m 0644 /tmp/prov11/config.py src/kalshi_predictor/config.py
install -m 0644 /tmp/prov11/routes.py src/kalshi_predictor/ui/routes.py
install -m 0644 /tmp/prov11/diagnostics.py src/kalshi_predictor/provenance/diagnostics.py
install -m 0644 /tmp/prov11/system_health.html src/kalshi_predictor/ui/templates/system_health.html
install -m 0644 /tmp/prov11/provenance_diagnostics.html src/kalshi_predictor/ui/templates/provenance_diagnostics.html
install -m 0644 /tmp/prov11/test_phase_prov11.py tests/test_phase_prov11.py
chown kalshi:kalshi src/kalshi_predictor/config.py src/kalshi_predictor/ui/routes.py \
  src/kalshi_predictor/provenance/diagnostics.py \
  src/kalshi_predictor/ui/templates/system_health.html \
  src/kalshi_predictor/ui/templates/provenance_diagnostics.html
./.venv/bin/python -m py_compile src/kalshi_predictor/config.py \
  src/kalshi_predictor/ui/routes.py src/kalshi_predictor/provenance/diagnostics.py
echo PROV11_BACKUP=$backup
