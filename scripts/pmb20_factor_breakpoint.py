from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.factor_breakpoint import (
    write_factor_isolated_breakpoint_attribution,
)

parser = argparse.ArgumentParser(description="Run local PMB-20 isolated-factor attribution")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb20"))
args = parser.parse_args()
print(write_factor_isolated_breakpoint_attribution(args.output_dir))
