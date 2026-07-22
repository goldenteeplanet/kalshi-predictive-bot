#!/usr/bin/env bash
set -Eeuo pipefail

APP_PATH=${APP_PATH:-/opt/kalshi-predictive-bot}
ENV_FILE=${ENV_FILE:-/etc/kalshi-bot/kalshi-bot.env}
UI_URL=${UI_URL:-http://127.0.0.1:8080/today}
WAIT_SECONDS=${WAIT_SECONDS:-600}
UI_WAIT_SECONDS=${UI_WAIT_SECONDS:-60}
TARGET_SHA=${1:-}

TIMERS=(
  kalshi-gh1-websocket-drain.timer
  kalshi-nyc-weather-runtime-refresh.timer
  kalshi-gh2-decision-refresh.timer
)
WRITER_SERVICES=(
  kalshi-gh1-websocket-drain.service
  kalshi-nyc-weather-runtime-refresh.service
  kalshi-gh2-decision-refresh.service
)
declare -A TIMER_WAS_ACTIVE=()
PREVIOUS_SHA=""
DEPLOY_STARTED=false

die() {
  echo "ERROR: $*" >&2
  exit 1
}

restore_timers() {
  for timer in "${TIMERS[@]}"; do
    if [[ ${TIMER_WAS_ACTIVE[$timer]:-false} == true ]]; then
      systemctl start "$timer" || true
    fi
  done
}

wait_for_ui() {
  local deadline=$((SECONDS + UI_WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    if curl --fail --silent --max-time 5 "$UI_URL" >/dev/null; then
      return 0
    fi
    sleep 2
  done
  die "UI did not become healthy at $UI_URL within ${UI_WAIT_SECONDS}s."
}

rollback_on_error() {
  set +e
  if [[ $DEPLOY_STARTED == true && -n $PREVIOUS_SHA ]]; then
    echo "Deployment failed; rolling $APP_PATH back to $PREVIOUS_SHA." >&2
    git -C "$APP_PATH" checkout --detach "$PREVIOUS_SHA"
    "$APP_PATH/.venv/bin/python" -m pip install --no-deps -e "$APP_PATH"
    systemctl restart kalshi-ui.service
    systemctl restart kalshi-gh1-websocket-watch.service
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT
  if (( rc != 0 )); then
    rollback_on_error
  fi
  restore_timers
  exit "$rc"
}

trap cleanup EXIT

[[ $EUID -eq 0 ]] || die "Run this handoff with sudo."
[[ $TARGET_SHA =~ ^[0-9a-f]{40}$ ]] || die "Pass one exact 40-character Git commit SHA."
[[ -d $APP_PATH/.git ]] || die "$APP_PATH is not a Git checkout."
[[ -x $APP_PATH/.venv/bin/kalshi-bot ]] || die "The repository virtualenv is missing."
[[ -f $ENV_FILE ]] || die "$ENV_FILE is missing."

echo "Paper-only guarded deployment of $TARGET_SHA"
echo "This command does not enable paper-order creation, exchange execution, or autopilot."

for timer in "${TIMERS[@]}"; do
  if systemctl is-active --quiet "$timer"; then
    TIMER_WAS_ACTIVE[$timer]=true
  else
    TIMER_WAS_ACTIVE[$timer]=false
  fi
done
systemctl stop "${TIMERS[@]}"

deadline=$((SECONDS + WAIT_SECONDS))
while true; do
  active_service=""
  for service in "${WRITER_SERVICES[@]}"; do
    if systemctl is-active --quiet "$service"; then
      active_service=$service
      break
    fi
  done
  [[ -z $active_service ]] && break
  (( SECONDS < deadline )) || die "Timed out waiting for $active_service to finish."
  echo "Waiting for in-flight writer service $active_service..."
  sleep 5
done

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

writer_status=$(cd "$APP_PATH" && .venv/bin/kalshi-bot db-writer-monitor --json)
grep -q '"safe_to_start_write": true' <<<"$writer_status" \
  || die "db-writer-monitor did not clear the deployment gate."
grep -q '"writer_count": 0' <<<"$writer_status" \
  || die "A SQLite writer is still active."

[[ -z $(git -C "$APP_PATH" status --porcelain) ]] \
  || die "$APP_PATH has uncommitted changes; deployment refused."
PREVIOUS_SHA=$(git -C "$APP_PATH" rev-parse HEAD)
git -C "$APP_PATH" fetch --prune origin
git -C "$APP_PATH" fetch origin "$TARGET_SHA"
git -C "$APP_PATH" cat-file -e "$TARGET_SHA^{commit}"

DEPLOY_STARTED=true
git -C "$APP_PATH" checkout --detach "$TARGET_SHA"
[[ $(git -C "$APP_PATH" rev-parse HEAD) == "$TARGET_SHA" ]] \
  || die "Checked-out commit does not match the requested SHA."
"$APP_PATH/.venv/bin/python" -m pip install --no-deps -e "$APP_PATH"

cd "$APP_PATH"
.venv/bin/kalshi-bot --help >/dev/null
.venv/bin/kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5 >/dev/null
post_install_writer_status=$(.venv/bin/kalshi-bot db-writer-monitor --json)
grep -q '"writer_count": 0' <<<"$post_install_writer_status" \
  || die "A writer appeared during read-only verification."

systemctl daemon-reload
systemctl restart kalshi-ui.service
systemctl restart kalshi-gh1-websocket-watch.service
wait_for_ui

DEPLOY_STARTED=false
echo "Verified commit: $(git -C "$APP_PATH" rev-parse HEAD)"
echo "UI check: HTTP success at $UI_URL"
echo "Writer gate: clear; paper-only safety remains locked."
