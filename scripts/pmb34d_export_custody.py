from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.export_custody import write_export_custody_preview


parser = argparse.ArgumentParser(description="Run local PMB-34D export custody certification")
parser.add_argument(
    "--custody", type=Path,
    default=Path("tests/fixtures/pmb34c/custody_manifest.json"),
)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb34d"))
args = parser.parse_args()
print(write_export_custody_preview(args.custody, args.output_dir))
