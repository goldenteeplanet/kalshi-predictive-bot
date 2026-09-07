#!/usr/bin/env bash
set -euo pipefail
envfile=/etc/kalshi-bot/kalshi-bot.env
backup=/etc/kalshi-bot/kalshi-bot.env.pre-prov7-20260717
cp -a "$envfile" "$backup"

if grep -q '^RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=' "$envfile"; then
  sed -i 's/^RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=.*/RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=true/' "$envfile"
else
  printf '\nRUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=true\n' >> "$envfile"
fi
chmod --reference="$backup" "$envfile"
chown --reference="$backup" "$envfile"

cd /opt/kalshi-predictive-bot
set -a
. "$envfile"
set +a
[[ "$RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED" == true ]]
[[ "${EXECUTION_ENABLED:-false}" == false ]]
.venv/bin/python - <<'PY'
from kalshi_predictor.config import Settings
s = Settings()
assert s.runtime_provenance_dual_write_enabled is True
assert s.execution_enabled is False
print("dual_write_enabled=true")
print("execution_enabled=false")
PY

cat > /root/prov7_disable_flag.sh <<'ROLLBACK'
#!/usr/bin/env bash
set -euo pipefail
sed -i 's/^RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=.*/RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=false/' /etc/kalshi-bot/kalshi-bot.env
systemctl restart kalshi-ui.service
systemctl restart kalshi-multicategory-refresh-scheduler.timer
ROLLBACK
chmod 700 /root/prov7_disable_flag.sh

systemctl start kalshi-ui.service
systemctl start kalshi-multicategory-refresh-scheduler.timer
systemctl start kalshi-multicategory-refresh-scheduler.service
printf 'env_backup=%s\nui=%s\nscheduler_timer=%s\nscheduler_service=%s\n' \
  "$backup" \
  "$(systemctl is-active kalshi-ui.service)" \
  "$(systemctl is-active kalshi-multicategory-refresh-scheduler.timer)" \
  "$(systemctl is-active kalshi-multicategory-refresh-scheduler.service)"
