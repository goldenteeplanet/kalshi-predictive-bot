from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.gate_margin import write_exact_gate_margin_report

parser = argparse.ArgumentParser(description="Run local PMB-13 gate-margin certification")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb13"))
args = parser.parse_args()
print(write_exact_gate_margin_report(args.output_dir))
