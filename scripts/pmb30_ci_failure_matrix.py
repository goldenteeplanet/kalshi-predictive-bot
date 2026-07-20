from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.benchmarking.ci_failure_matrix import (
    write_offline_ci_failure_mode_matrix,
)

parser = argparse.ArgumentParser(description="Run local PMB-30 CI failure-mode matrix")
parser.add_argument("--project-root", type=Path, default=Path("."))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb30"))
args = parser.parse_args()
path = write_offline_ci_failure_mode_matrix(args.project_root, args.output_dir)
report = json.loads(path.read_text(encoding="utf-8"))
print(path)
raise SystemExit(0 if report["summary"]["all_failures_detected"] else 1)
