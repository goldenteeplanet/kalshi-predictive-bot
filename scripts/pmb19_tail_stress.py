from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.tail_stress import write_tail_stress_breakpoint_search

parser = argparse.ArgumentParser(description="Run local PMB-19 tail-stress breakpoint search")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb19"))
args = parser.parse_args()
print(write_tail_stress_breakpoint_search(args.output_dir))
