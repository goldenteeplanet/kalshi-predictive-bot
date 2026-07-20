from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.interaction_boundary import (
    write_interaction_boundary_refinement,
)

parser = argparse.ArgumentParser(description="Run local PMB-22 boundary refinement")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb22"))
parser.add_argument(
    "--golden-path",
    type=Path,
    default=Path("tests/golden/pmb22_interaction_boundary_summary.json"),
)
args = parser.parse_args()
print(write_interaction_boundary_refinement(args.output_dir, golden_path=args.golden_path))
