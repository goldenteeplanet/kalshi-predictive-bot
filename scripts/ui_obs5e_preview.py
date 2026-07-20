from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.ui.cloud_snapshot_parity import certify_cloud_snapshot_parity
from kalshi_predictor.ui.live_status_collector import collect_live_snapshot


def captured_runner(command: list[str], _timeout: int) -> str:
    if command[:2] == ["kalshi-bot", "db-writer-monitor"]:
        return "Current writer PID: none\nSafe to start another write job: yes\n"
    if command[:2] == ["kalshi-bot", "db-locks"]:
        return "Database lock diagnostics: CLEAR\nOpen DB holders: none visible\n"
    states = {
        "kalshi-r5-bounded.service": "ActiveState=inactive\nSubState=dead\nExecMainPID=0\nResult=success\n",
        "kalshi-r5-bounded.timer": "ActiveState=inactive\nSubState=dead\nUnitFileState=enabled\n",
        "kalshi-r5-watcher.service": "ActiveState=inactive\nUnitFileState=disabled\n",
    }
    return states[command[2]]


root = Path("reports/phase_ui_obs5e")
root.mkdir(parents=True, exist_ok=True)
snapshot = collect_live_snapshot(
    runner=captured_runner,
    backup_root=root / "empty_backup_fixture",
    reports_root=Path("reports"),
    roadmap_path=Path("reports/phase_objective_status/objective_20_phase_status_20260719.json"),
    r5_certification_path=Path("reports/phase_r5_recovery9/r5_recovery9_deployment_certification.json"),
)
snapshot_path = root / "ui_obs5e_repaired_snapshot.json"
snapshot_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
authoritative = {
    "execution_enabled": False, "lock_status": "CLEAR", "safe_to_start_write": True,
    "bounded_service": "kalshi-r5-bounded.service", "bounded_timer_enabled": True,
    "bounded_timer_active": False, "legacy_enabled": False, "legacy_active": False,
}
report = certify_cloud_snapshot_parity(snapshot, authoritative, reference_time=datetime.now(UTC))
report.update({
    "phase": "UI-OBS-5E", "mode": "LOCAL_COLLECTOR_SCHEMA_REFRESH",
    "source_snapshot": str(snapshot_path), "cloud_access": False,
    "deployment_performed": False,
})
output = root / "ui_obs5e_collector_schema_refresh_certification.json"
output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(output)
