from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.incident_resolution import build_incident_resolution_preview
from kalshi_predictor.ui.progress_history import record_progress_snapshot


parser = argparse.ArgumentParser(description="Run local UI-OBS-2D incident resolution preview")
parser.add_argument("--sequence", type=Path, default=Path("tests/fixtures/ui_obs2c/history_sequence.json"))
parser.add_argument("--acknowledgments", type=Path, default=Path("tests/fixtures/ui_obs2d/acknowledgments.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2d"))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
history = args.output_dir / "progress_snapshot.json.history.json"
for snapshot in json.loads(args.sequence.read_text(encoding="utf-8")):
    record_progress_snapshot(snapshot, history)
preview = build_incident_resolution_preview(history, args.acknowledgments, as_of="2026-07-18T09:10:00Z")
report = {"phase":"UI-OBS-2D","mode":"LOCAL_READ_ONLY_INCIDENT_RESOLUTION_PREVIEW","database_access":False,"database_writes":0,"cloud_access":False,"execution_changed":False,"preview":preview}
path = args.output_dir / "ui_obs2d_incident_resolution_preview.json"
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(path)
