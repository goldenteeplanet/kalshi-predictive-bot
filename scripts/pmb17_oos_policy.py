from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.oos_policy import (
    write_oos_robust_policy_validation,
)

parser = argparse.ArgumentParser(description="Run local PMB-17 OOS validation")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb17"))
args = parser.parse_args()
print(write_oos_robust_policy_validation(args.output_dir))
