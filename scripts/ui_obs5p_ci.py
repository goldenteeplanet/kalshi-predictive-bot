from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.phase_ui_obs5p import certify_timeline_export, verify_timeline_bundle

parser = argparse.ArgumentParser(description="Run the offline UI-OBS-5P certification gate")
parser.add_argument("--history", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_ui_obs5p"))
args = parser.parse_args()

try:
    manifest = certify_timeline_export(args.history, args.output_dir)
    verification = verify_timeline_bundle(args.output_dir)
except (OSError, ValueError, json.JSONDecodeError) as exc:
    print(json.dumps({"status": "FAILED", "failures": [type(exc).__name__]}, sort_keys=True))
    raise SystemExit(2) from None

result = {
    "status": "PASSED"
    if manifest["status"] == "PASSED" and verification["status"] == "PASSED"
    else "FAILED",
    "manifest": manifest,
    "verification": verification,
}
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result["status"] == "PASSED" else 2)
