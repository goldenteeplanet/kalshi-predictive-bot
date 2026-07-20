from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.ui.process_progress import normalize_process_progress


parser = argparse.ArgumentParser(description="Certify UI-OBS-2M process progress and ETA accuracy")
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2m"))
args = parser.parse_args()
reference = datetime(2026, 7, 18, 0, 10, tzinfo=UTC)
scenarios = {
    "calculable":{"name":"bounded-job","pid":42,"state":"RUNNING","started_at":"2026-07-18T00:00:00Z","updated_at":"2026-07-18T00:10:00Z","completed_units":2,"total_units":10,"stage":"  forecast   batch  "},
    "unknown_eta":{"name":"opaque-job","pid":43,"state":"RUNNING","updated_at":"2026-07-18T00:10:00Z"},
    "stale":{"name":"stale-job","pid":44,"state":"RUNNING","updated_at":"2026-07-17T23:50:00Z"},
    "complete":{"name":"done","state":"PASSED","completed_units":10,"total_units":10,"completion_evidence":"report.json"},
}
results = {}
for name, scenario in scenarios.items():
    process, diagnostics = normalize_process_progress(scenario, reference_time=reference)
    results[name] = {"process":process,"diagnostics":diagnostics}
checks = {"calculable_eta":results["calculable"]["process"]["estimated_remaining"]=="40m","unknown_not_invented":results["unknown_eta"]["process"]["estimated_remaining"]=="unknown","stale_blocked":results["stale"]["process"]["state"]=="BLOCKED","complete_zero_eta":results["complete"]["process"]["estimated_remaining_seconds"]==0}
report = {"phase":"UI-OBS-2M","mode":"LOCAL_PROCESS_PROGRESS_AND_ETA_ACCURACY","status":"PASSED" if all(checks.values()) else "FAILED","checks":checks,"scenarios":results,"database_access":False,"cloud_access":False,"deployment_performed":False,"execution_changed":False}
args.output_dir.mkdir(parents=True, exist_ok=True)
path = args.output_dir / "ui_obs2m_process_progress_eta_certification.json"
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(path)
raise SystemExit(0 if report["status"] == "PASSED" else 1)
