from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.export_drift_trend import write_export_drift_trend_preview


parser = argparse.ArgumentParser(description="Run local PMB-34F multi-bundle drift trend")
parser.add_argument(
    "--manifest", type=Path,
    default=Path("tests/fixtures/pmb34f/trend_manifest.json"),
)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb34f"))
args = parser.parse_args()
print(write_export_drift_trend_preview(args.manifest, args.output_dir))
