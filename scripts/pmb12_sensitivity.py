from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.sensitivity import write_sensitivity_grid

parser = argparse.ArgumentParser(description="Run local PMB-12 sensitivity grid")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb12"))
args = parser.parse_args()
print(write_sensitivity_grid(args.output_dir))
