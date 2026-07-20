from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.accessibility_certification import write_accessibility_certification


parser = argparse.ArgumentParser(description="Run local UI-OBS-2K accessibility certification")
parser.add_argument("--project-root", type=Path, default=Path("."))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2k"))
args = parser.parse_args()
path = write_accessibility_certification(args.project_root, args.output_dir)
report = json.loads(path.read_text(encoding="utf-8"))
print(path)
raise SystemExit(0 if report["status"] == "PASSED" else 1)
