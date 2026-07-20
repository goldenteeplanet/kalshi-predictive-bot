from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.stress_guard import (
    write_stress_aware_allocation_guard_preview,
)

parser = argparse.ArgumentParser(description="Run local PMB-23 stress guard preview")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb23"))
args = parser.parse_args()
print(write_stress_aware_allocation_guard_preview(args.output_dir))
