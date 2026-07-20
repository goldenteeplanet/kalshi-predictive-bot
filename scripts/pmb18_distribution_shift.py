from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.distribution_shift import (
    write_distribution_shift_stress_validation,
)

parser = argparse.ArgumentParser(description="Run local PMB-18 distribution-shift stress validation")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb18"))
args = parser.parse_args()
print(write_distribution_shift_stress_validation(args.output_dir))
