from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.benchmarking.exposure_ci import (
    run_offline_exposure_certification_ci_gate,
)

parser = argparse.ArgumentParser(description="Run local PMB-28 exposure certification CI gate")
parser.add_argument("--project-root", type=Path, default=Path("."))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb28"))
parser.add_argument("--golden-path", type=Path, default=None)
args = parser.parse_args()
path, exit_code = run_offline_exposure_certification_ci_gate(
    args.project_root, args.output_dir, golden_path=args.golden_path
)
payload = json.loads(path.read_text(encoding="utf-8"))
print(json.dumps({
    "report": str(path),
    "passed": payload["summary"]["passed"],
    "exit_code": exit_code,
}, sort_keys=True))
raise SystemExit(exit_code)
