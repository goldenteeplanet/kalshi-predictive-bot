from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.certification_status import build_ci_certification_status


parser = argparse.ArgumentParser(description="Generate the read-only UI-OBS-2J certification dashboard preview")
parser.add_argument("--reports-root", type=Path, default=Path("reports"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2j"))
args = parser.parse_args()
status = build_ci_certification_status(args.reports_root)
report = {
    "phase": "UI-OBS-2J",
    "mode": "CI_CERTIFICATION_STATUS_DASHBOARD_PREVIEW",
    "status": status["status"],
    "dashboard_path": "/system/progress",
    "api_path": "/api/system/progress",
    "certification": status,
    "database_access": False,
    "database_writes": 0,
    "cloud_access": False,
    "runtime_controls": False,
    "actual_notifications_sent": 0,
    "execution_changed": False,
}
args.output_dir.mkdir(parents=True, exist_ok=True)
path = args.output_dir / "ui_obs2j_ci_certification_dashboard_preview.json"
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(path)
raise SystemExit(0 if report["status"] == "PASSED" else 1)
