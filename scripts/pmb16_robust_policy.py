from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.robust_policy import (
    write_robust_zone_policy_comparison,
)

parser = argparse.ArgumentParser(description="Run local PMB-16 robust-zone policy")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb16"))
args = parser.parse_args()
print(write_robust_zone_policy_comparison(args.output_dir))
