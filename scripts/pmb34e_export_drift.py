from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.export_drift import write_export_drift_preview


parser = argparse.ArgumentParser(description="Run local PMB-34E export drift certification")
parser.add_argument(
    "--comparison", type=Path,
    default=Path("tests/fixtures/pmb34e/comparison.json"),
)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb34e"))
args = parser.parse_args()
print(write_export_drift_preview(args.comparison, args.output_dir))
