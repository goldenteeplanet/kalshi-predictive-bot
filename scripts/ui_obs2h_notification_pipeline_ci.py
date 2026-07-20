from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.notification_pipeline_ci import run_notification_pipeline_ci


parser = argparse.ArgumentParser(description="Run offline UI-OBS-2H notification pipeline CI gate")
parser.add_argument("--reports-root", type=Path, default=Path("reports"))
parser.add_argument("--golden", type=Path, default=Path("tests/golden/ui_obs2h_notification_pipeline_golden.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2h"))
args = parser.parse_args()
result = run_notification_pipeline_ci(args.reports_root, args.golden)
args.output_dir.mkdir(parents=True, exist_ok=True)
path = args.output_dir / "ui_obs2h_notification_pipeline_ci.json"
path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(path)
raise SystemExit(result["exit_code"])
