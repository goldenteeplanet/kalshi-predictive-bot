from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.oos_exposure_guard import (
    write_oos_exposure_guard_validation,
)

parser = argparse.ArgumentParser(description="Run local PMB-25 OOS exposure validation")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb25"))
args = parser.parse_args()
print(write_oos_exposure_guard_validation(args.output_dir))
