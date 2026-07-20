from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.runtime_compatibility import (
    write_runtime_field_compatibility_preview,
)

parser = argparse.ArgumentParser(description="Run local PMB-34 runtime compatibility preview")
parser.add_argument(
    "--fixtures",
    type=Path,
    default=Path("tests/fixtures/pmb34/runtime_ranking_risk_exports.json"),
)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb34"))
args = parser.parse_args()
print(write_runtime_field_compatibility_preview(args.fixtures, args.output_dir))
