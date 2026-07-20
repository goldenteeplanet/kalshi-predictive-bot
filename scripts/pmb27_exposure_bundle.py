from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.exposure_bundle import (
    write_exposure_guard_certification_bundle,
)

parser = argparse.ArgumentParser(description="Build local PMB-27 golden certification bundle")
parser.add_argument("--project-root", type=Path, default=Path("."))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb27"))
parser.add_argument(
    "--golden-path",
    type=Path,
    default=Path("tests/golden/pmb27_exposure_guard_bundle_summary.json"),
)
args = parser.parse_args()
print(write_exposure_guard_certification_bundle(
    args.project_root, args.output_dir, golden_path=args.golden_path
))
