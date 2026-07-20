from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.exposure_stability import (
    write_multi_seed_exposure_stability_census,
)

parser = argparse.ArgumentParser(description="Run local PMB-26 multi-seed stability census")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb26"))
args = parser.parse_args()
print(write_multi_seed_exposure_stability_census(args.output_dir))
