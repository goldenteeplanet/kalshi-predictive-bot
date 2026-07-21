#!/usr/bin/env bash
set -euo pipefail

project=/opt/kalshi-predictive-bot
unit=/etc/systemd/system/kalshi-ui-status-collector.service
module=$project/src/kalshi_predictor/ui/live_status_collector.py
collector=$project/scripts/ui_obs3b_live_status_collector.py
snapshot=$project/reports/ui_obs_live/progress_snapshot.json
stamp=$(date -u +%Y%m%dT%H%M%SZ)
rollback=/mnt/kalshi-backup-02/ui_obs5f_retry/$stamp
timer_was_active=false

mkdir -p "$rollback"
cp --preserve=all "$module" "$rollback/live_status_collector.py"
cp --preserve=all "$collector" "$rollback/ui_obs3b_live_status_collector.py"
cp --preserve=all "$unit" "$rollback/kalshi-ui-status-collector.service"
sha256sum "$rollback"/*.py "$rollback"/*.service >"$rollback/MANIFEST.sha256"
sha256sum -c "$rollback/MANIFEST.sha256"

rollback_now() {
  set +e
  install -m 0644 "$rollback/live_status_collector.py" "$module"
  install -m 0755 "$rollback/ui_obs3b_live_status_collector.py" "$collector"
  install -m 0644 "$rollback/kalshi-ui-status-collector.service" "$unit"
  systemctl daemon-reload
  [[ "$timer_was_active" == true ]] && systemctl start kalshi-ui-status-collector.timer
  echo "ROLLED_BACK=$rollback"
}
fail_and_rollback() {
  status=$?
  trap - ERR
  rollback_now
  exit "$status"
}
trap 'fail_and_rollback' ERR

grep -qx 'EXECUTION_ENABLED=false' /etc/kalshi-bot/kalshi-bot.env
[[ "$(systemctl is-active kalshi-r5-watcher.service || true)" == inactive ]]
[[ "$(systemctl is-enabled kalshi-r5-watcher.service || true)" == disabled ]]
[[ "$(systemctl is-active kalshi-ui-status-collector.service || true)" == inactive ]]
[[ "$(systemctl is-active kalshi-ui-status-collector.timer || true)" == active ]] && timer_was_active=true
systemd-analyze verify /tmp/kalshi-ui-status-collector.service

systemctl stop kalshi-ui-status-collector.timer
[[ "$(systemctl is-active kalshi-ui-status-collector.service || true)" == inactive ]]
install -m 0644 /tmp/ui_obs5f_live_status_collector.py "$module"
install -m 0755 /tmp/ui_obs5f_collector.py "$collector"
install -m 0644 /tmp/kalshi-ui-status-collector.service "$unit"
systemctl daemon-reload
systemd-analyze verify "$unit"
grep -q -- '--service kalshi-r5-bounded.service' "$unit"
! grep -q -- '--service kalshi-r5-watcher.service' "$unit"

before=$(stat -c %Y "$snapshot" 2>/dev/null || echo 0)
systemctl start kalshi-ui-status-collector.service
[[ "$(systemctl is-active kalshi-ui-status-collector.service || true)" == inactive ]]
[[ "$(systemctl show kalshi-ui-status-collector.service -p Result --value)" == success ]]
after=$(stat -c %Y "$snapshot")
(( after > before ))

"$project/.venv/bin/python" - "$snapshot" <<'PY'
import json, sys
p=json.load(open(sys.argv[1], encoding="utf-8"))
assert p["execution_enabled"] is False
assert p["collector"]["read_only"] is True
assert p["collector"]["database_writes"] == 0
assert p["scheduler"]["service"] == "kalshi-r5-bounded.service"
assert p["scheduler"]["timer"] == "kalshi-r5-bounded.timer"
assert p["scheduler"]["legacy_watcher_enabled"] is False
assert p["scheduler"]["legacy_watcher_active"] is False
assert len(p["phase_roadmap"]) == 20
assert p["r5_recovery9_certification"]["status"] == "PASSED"
assert p["r5_recovery9_certification"]["rollback_verified"] is True
assert p["prov14b"]["state"] in {"WAITING", "RUNNING", "QUEUED"}
PY
grep -qx 'EXECUTION_ENABLED=false' /etc/kalshi-bot/kalshi-bot.env
[[ "$timer_was_active" == true ]] && systemctl start kalshi-ui-status-collector.timer
[[ "$(systemctl is-active kalshi-ui-status-collector.timer || true)" == active ]]
trap - ERR
echo "DEPLOYMENT_PASSED=true"
echo "ROLLBACK_PATH=$rollback"
sha256sum "$module" "$collector" "$unit" "$snapshot"
