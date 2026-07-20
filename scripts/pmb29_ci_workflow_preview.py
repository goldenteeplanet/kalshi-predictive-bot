from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.ci_workflow_preview import (
    write_ci_workflow_integration_preview,
)

parser = argparse.ArgumentParser(description="Generate local PMB-29 CI workflow preview")
parser.add_argument("--project-root", type=Path, default=Path("."))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb29"))
args = parser.parse_args()
print(write_ci_workflow_integration_preview(args.project_root, args.output_dir))
