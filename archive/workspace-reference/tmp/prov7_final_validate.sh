#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
DB=/var/lib/kalshi-bot/kalshi_phase1.db

[[ "$RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED" == true ]]
[[ "${EXECUTION_ENABLED:-false}" == false ]]
[[ -f /etc/kalshi-bot/kalshi-bot.env.pre-prov7-20260717 ]]
[[ -x /root/prov7_disable_flag.sh ]]
[[ -d /root/prov7_code_backup_20260717 ]]

.venv/bin/python - <<'PY'
import hashlib, json, sqlite3
db = sqlite3.connect('/var/lib/kalshi-bot/kalshi_phase1.db')
db.row_factory = sqlite3.Row
row = db.execute('SELECT * FROM runtime_provenance_events ORDER BY id DESC LIMIT 1').fetchone()
assert row is not None
raw = json.loads(row['raw_json'])
digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
assert digest == row['provenance_digest']
assert db.execute('SELECT COUNT(*) FROM forecasts WHERE id=?', (row['forecast_id'],)).fetchone()[0] == 1
print('digest_verified=true')
print('legacy_forecast_link_verified=true')
print(f"provenance_event_id={row['id']}")
PY

systemctl reset-failed kalshi-multicategory-refresh-scheduler.service || true
systemctl start kalshi-ui.service
systemctl start kalshi-multicategory-refresh-scheduler.timer

printf 'revision=%s\nevents=%s\ndual_write=true\nexecution=false\nui=%s\nscheduler_timer=%s\nrollback_env_backup=true\nrollback_script=true\ncode_backup=true\n' \
  "$(.venv/bin/alembic current 2>/dev/null | tail -1)" \
  "$(sqlite3 "$DB" 'SELECT COUNT(*) FROM runtime_provenance_events;')" \
  "$(systemctl is-active kalshi-ui.service)" \
  "$(systemctl is-active kalshi-multicategory-refresh-scheduler.timer)"
.venv/bin/kalshi-bot db-writer-monitor --json
.venv/bin/kalshi-bot db-locks || true
