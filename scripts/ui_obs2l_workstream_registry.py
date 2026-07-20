from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.workstream_registry import normalize_workstream_registry


parser = argparse.ArgumentParser(description="Certify the UI-OBS-2L workstream registry")
parser.add_argument("--snapshot", type=Path, default=Path("tests/fixtures/ui_obs1/progress_snapshot.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2l"))
args = parser.parse_args()
payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
registry = normalize_workstream_registry(payload)
report = {"phase":"UI-OBS-2L","mode":"LOCAL_COMPLETE_WORKSTREAM_AND_PHASE_REGISTRY","status":"PASSED" if registry["coverage"]["complete"] else "FAILED","registry":registry,"database_access":False,"cloud_access":False,"deployment_performed":False,"execution_changed":False}
args.output_dir.mkdir(parents=True, exist_ok=True)
path = args.output_dir / "ui_obs2l_workstream_registry_certification.json"
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(path)
raise SystemExit(0 if report["status"] == "PASSED" else 1)
