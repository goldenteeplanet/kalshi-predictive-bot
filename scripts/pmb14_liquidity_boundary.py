from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.liquidity_boundary import (
    write_liquidity_boundary_sweep,
)

parser = argparse.ArgumentParser(description="Run local PMB-14 liquidity boundary sweep")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb14"))
args = parser.parse_args()
print(write_liquidity_boundary_sweep(args.output_dir))
