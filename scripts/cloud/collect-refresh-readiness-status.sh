#!/usr/bin/env bash
set -euo pipefail

APP_PATH=${APP_PATH:-/opt/kalshi-predictive-bot}
GH2_ROOT=${GH2_ROOT:-/var/lib/kalshi-bot-gh2}
OUTPUT_PATH=${1:-$GH2_ROOT/reports/authoritative_cloud_status.json}
REFRESH_PATH=$GH2_ROOT/reports/gh2_active_candidate_refresh.json

cd "$APP_PATH"
deployment_sha=$(git rev-parse HEAD)
host_id=$(hostname)
service_status=$(systemctl is-active kalshi-gh2-decision-refresh.service || true)
timer_status=$(systemctl is-active kalshi-gh2-decision-refresh.timer || true)

DEPLOYMENT_SHA="$deployment_sha" \
HOST_ID="$host_id" \
SERVICE_STATUS="$service_status" \
TIMER_STATUS="$timer_status" \
REFRESH_PATH="$REFRESH_PATH" \
OUTPUT_PATH="$OUTPUT_PATH" \
  .venv/bin/python - <<'PY'
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

refresh_path = Path(os.environ["REFRESH_PATH"])
try:
    refresh = json.loads(refresh_path.read_text(encoding="utf-8"))
    refresh_bytes = refresh_path.read_bytes()
except (FileNotFoundError, json.JSONDecodeError, OSError):
    refresh = {}
    refresh_bytes = b""

snapshot = {
    "deployment_commit_sha": os.environ["DEPLOYMENT_SHA"],
    "host_id": os.environ["HOST_ID"],
    "environment": "paper-cloud",
    "service_status": os.environ["SERVICE_STATUS"],
    "timer_status": os.environ["TIMER_STATUS"],
    "last_successful_refresh": refresh.get("generated_at"),
    "collected_at": datetime.now(UTC).isoformat(),
    "artifact_hashes": {
        "gh2_active_candidate_refresh.json": (
            hashlib.sha256(refresh_bytes).hexdigest() if refresh_bytes else None
        )
    },
}
canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
envelope = {
    "schema_version": "refresh-control-plane-v1",
    "sha256": hashlib.sha256(canonical).hexdigest(),
    "snapshot": snapshot,
}
path = Path(os.environ["OUTPUT_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
temporary.replace(path)
PY

echo "Wrote checksummed read-only refresh status to $OUTPUT_PATH"
