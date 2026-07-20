from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.ui.live_roadmap_status import build_live_roadmap_status

parser = argparse.ArgumentParser(description="Build the local UI-OBS-5B integration preview.")
parser.add_argument("--roadmap", type=Path, default=Path("reports/phase_objective_status/objective_20_phase_status_20260719.json"))
parser.add_argument("--certification", type=Path, default=Path("reports/phase_r5_recovery9/r5_recovery9_deployment_certification.json"))
parser.add_argument("--output", type=Path, default=Path("reports/phase_ui_obs5b/ui_obs5b_live_roadmap_scheduler_preview.json"))
parser.add_argument("--snapshot-output", type=Path, default=Path("reports/phase_ui_obs5b/ui_obs5b_browser_snapshot.json"))
args = parser.parse_args()
roadmap = json.loads(args.roadmap.read_text(encoding="utf-8"))
cert = json.loads(args.certification.read_text(encoding="utf-8"))
reference = datetime(2026, 7, 19, 12, 6, 26, tzinfo=UTC)
payload = {
    "generated_at": "2099-01-01T00:00:00Z",
    "phase_roadmap": roadmap["phases"],
    "execution_enabled": False,
    "active_process": {"state": "WAITING", "name": "Bounded scheduler", "stage": "next_cycle", "runtime": "idle"},
    "writer": {"safe_to_start_write": True, "lock_status": "CLEAR"},
    "scheduler": {
        "state": "WAITING", "timer": "kalshi-r5-bounded.timer", "next_run": "2026-07-19T12:19:58Z",
        "current_cycle": None, "runtime_seconds": 0, "memory_current_bytes": None,
        "memory_peak_bytes": None, "heartbeat": {}, "legacy_watcher_enabled": False,
        "legacy_watcher_active": False, "last_result": "success",
    },
    "r5_recovery9_certification": {
        "status": cert["status"], "rollback_verified": cert["gates"]["rollback_hash_verified"],
        "rollback_path": cert["rollback"]["path"],
    },
    "prov14b": {"state": "QUEUED", "reason": "Waiting for first bounded scheduler cycle and sole-writer isolation."},
}
args.snapshot_output.parent.mkdir(parents=True, exist_ok=True)
args.snapshot_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
report = build_live_roadmap_status(payload, reference_time=reference)
report.update({"phase": "UI-OBS-5B", "mode": "LOCAL_READ_ONLY_INTEGRATION_PREVIEW", "cloud_writes": 0, "service_controls": 0})
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(args.output)
