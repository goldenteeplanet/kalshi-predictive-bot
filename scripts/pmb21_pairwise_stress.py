from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.pairwise_stress import (
    write_pairwise_stress_interaction_matrix,
)

parser = argparse.ArgumentParser(description="Run local PMB-21 pairwise stress matrix")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb21"))
args = parser.parse_args()
print(write_pairwise_stress_interaction_matrix(args.output_dir))
