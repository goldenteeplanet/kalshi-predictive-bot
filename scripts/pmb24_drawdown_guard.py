from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.drawdown_guard import (
    write_drawdown_aware_guard_refinement,
)

parser = argparse.ArgumentParser(description="Run local PMB-24 drawdown guard refinement")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb24"))
args = parser.parse_args()
print(write_drawdown_aware_guard_refinement(args.output_dir))
