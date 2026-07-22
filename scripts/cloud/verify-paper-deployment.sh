#!/usr/bin/env bash
set -euo pipefail

APP_PATH=${APP_PATH:-/opt/kalshi-predictive-bot}
ENV_FILE=${ENV_FILE:-/etc/kalshi-bot/kalshi-bot.env}
UI_URL=${UI_URL:-http://127.0.0.1:8080/today}
GH1_STATUS_PATH=${GH1_STATUS_PATH:-/var/lib/kalshi-bot-gh1/watch/status.json}
EXPECTED_SHA=${1:-}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ $EXPECTED_SHA =~ ^[0-9a-f]{40}$ ]] \
  || die "Pass the exact deployed 40-character Git commit SHA."
[[ -d $APP_PATH/.git ]] || die "$APP_PATH is not a Git checkout."
[[ -x $APP_PATH/.venv/bin/kalshi-bot ]] || die "Repository virtualenv is missing."
[[ -f $ENV_FILE ]] || die "$ENV_FILE is missing."

deployed_sha=$(git -C "$APP_PATH" rev-parse HEAD)
[[ $deployed_sha == "$EXPECTED_SHA" ]] \
  || die "Deployed SHA $deployed_sha does not match $EXPECTED_SHA."
curl --fail --silent --show-error --max-time 10 "$UI_URL" >/dev/null \
  || die "UI health check failed at $UI_URL."

required_active_units=(
  kalshi-ui.service
  kalshi-gh1-websocket-watch.service
  kalshi-gh1-websocket-drain.timer
  kalshi-gh2-decision-refresh.timer
  kalshi-nyc-weather-runtime-refresh.timer
)
for unit in "${required_active_units[@]}"; do
  systemctl is-active --quiet "$unit" || die "$unit is not active."
done

systemctl is-active --quiet kalshi-r5-bounded.timer \
  && die "Legacy kalshi-r5-bounded.timer is active."
systemctl is-enabled --quiet kalshi-r5-bounded.timer \
  && die "Legacy kalshi-r5-bounded.timer is enabled."

active_writer_services=()
for service in \
  kalshi-gh1-websocket-drain.service \
  kalshi-gh2-decision-refresh.service \
  kalshi-nyc-weather-runtime-refresh.service \
  kalshi-r5-bounded.service; do
  if systemctl is-active --quiet "$service"; then
    active_writer_services+=("$service")
  fi
done
(( ${#active_writer_services[@]} <= 1 )) \
  || die "Multiple writer services are active: ${active_writer_services[*]}"
[[ ${active_writer_services[0]:-} != kalshi-r5-bounded.service ]] \
  || die "Legacy R5 writer service owns the database."

gh2_environment=$(systemctl show --property=Environment --value \
  kalshi-gh2-decision-refresh.service)
[[ $gh2_environment == *"EXECUTION_ENABLED=false"* ]] \
  || die "GH-2 exchange execution safety is not false."
[[ $gh2_environment == *"AUTOPILOT_ENABLED=false"* ]] \
  || die "GH-2 autopilot safety is not false."
[[ $gh2_environment == *"EXECUTION_KILL_SWITCH=true"* ]] \
  || die "GH-2 execution kill switch is not true."

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
writer_status=$(cd "$APP_PATH" && .venv/bin/kalshi-bot db-writer-monitor --json)
WRITER_STATUS="$writer_status" "$APP_PATH/.venv/bin/python" - <<'PY'
import json
import os

payload = json.loads(os.environ["WRITER_STATUS"])
writer_count = int(payload.get("writer_count") or 0)
if writer_count > 1:
    raise SystemExit(f"Multiple SQLite writers detected: {writer_count}")
print(f"Writer monitor: {writer_count} active writer(s)")
PY

GH1_STATUS_PATH="$GH1_STATUS_PATH" "$APP_PATH/.venv/bin/python" - <<'PY'
import json
import os
from datetime import UTC, datetime
from pathlib import Path

path = Path(os.environ["GH1_STATUS_PATH"])
payload = json.loads(path.read_text(encoding="utf-8"))
state = str(payload.get("state") or "UNKNOWN")
allowed = {"CONNECTING", "DISCOVERING_QUOTED_BOOKS", "STREAMING", "STREAM_CYCLE_COMPLETE"}
if state not in allowed:
    raise SystemExit(f"GH-1 reconnect state is unhealthy: {state}")
generated_at = datetime.fromisoformat(str(payload["generated_at"]).replace("Z", "+00:00"))
age_seconds = (datetime.now(UTC) - generated_at).total_seconds()
if age_seconds > 600:
    raise SystemExit(f"GH-1 status is stale: {age_seconds:.0f}s")
if int(payload.get("consecutive_failures") or 0) >= 3:
    raise SystemExit("GH-1 has at least three consecutive failures.")
print(
    f"GH-1 reconnect health: {state}; "
    f"{int(payload.get('reconnect_count') or 0)} reconnect(s); "
    f"status age {age_seconds:.0f}s"
)
PY

echo "Verified deployment: $deployed_sha"
echo "UI: HTTP success at $UI_URL"
echo "Timers: GH-1 drain, GH-2, and weather active; legacy R5 disabled"
echo "Safety: exchange execution disabled; autopilot disabled; kill switch active"
