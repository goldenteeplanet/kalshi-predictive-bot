from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.notification_pipeline_workflow import write_workflow_preview


parser = argparse.ArgumentParser(description="Certify the local UI-OBS-2I workflow preview")
parser.add_argument("--project-root", type=Path, default=Path("."))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2i"))
args = parser.parse_args()
path = write_workflow_preview(args.project_root, args.output_dir)
report = json.loads(path.read_text(encoding="utf-8"))
print(path)
raise SystemExit(0 if report["status"] == "PASSED" else 1)
