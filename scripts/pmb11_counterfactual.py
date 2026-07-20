from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.counterfactual import (
    write_counterfactual_model_comparison,
)

parser = argparse.ArgumentParser(description="Run local PMB-11 counterfactual replay")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb11"))
args = parser.parse_args()
print(write_counterfactual_model_comparison(args.output_dir))
