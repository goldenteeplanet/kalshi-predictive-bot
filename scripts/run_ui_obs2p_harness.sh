#!/usr/bin/env bash
set -euo pipefail
export KALSHI_PROGRESS_SNAPSHOT_PATH=reports/ui_obs2p/active_snapshot.json
export KALSHI_CERTIFICATION_REPORTS_ROOT=reports/ui_obs2p/certifications
exec .venv/bin/python -m uvicorn ui_obs2p_harness_app:app --app-dir scripts --host 0.0.0.0 --port 8765
