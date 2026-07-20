from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.joint_surface import (
    write_joint_robust_decision_surface,
)

parser = argparse.ArgumentParser(description="Run local PMB-15 joint decision surface")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb15"))
args = parser.parse_args()
print(write_joint_robust_decision_surface(args.output_dir))
