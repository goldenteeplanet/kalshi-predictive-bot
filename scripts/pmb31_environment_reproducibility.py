from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.environment_reproducibility import (
    write_cross_environment_reproducibility_preview,
)

parser = argparse.ArgumentParser(description="Run local PMB-31 environment preview")
parser.add_argument("--project-root", type=Path, default=Path("."))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb31"))
args = parser.parse_args()
print(write_cross_environment_reproducibility_preview(args.project_root, args.output_dir))
