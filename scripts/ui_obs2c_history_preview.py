from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.progress_history import load_progress_timeline, record_progress_snapshot


parser = argparse.ArgumentParser(description="Run local UI-OBS-2C history preview")
parser.add_argument("--sequence", type=Path, default=Path("tests/fixtures/ui_obs2c/history_sequence.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2c"))
args = parser.parse_args()
sequence = json.loads(args.sequence.read_text(encoding="utf-8"))
args.output_dir.mkdir(parents=True, exist_ok=True)
history_path = args.output_dir / "progress_snapshot.json.history.json"
results = [record_progress_snapshot(snapshot, history_path, limit=10) for snapshot in sequence]
timeline = load_progress_timeline(history_path)
timeline["history_path"] = history_path.name
report = {
    "phase":"UI-OBS-2C","mode":"LOCAL_BOUNDED_HISTORY_INCIDENT_TIMELINE_PREVIEW",
    "database_access":False,"database_writes":0,"cloud_access":False,"execution_changed":False,
    "appends":[item["appended"] for item in results],"timeline":timeline,
}
path = args.output_dir / "ui_obs2c_history_incident_timeline_preview.json"
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(path)
