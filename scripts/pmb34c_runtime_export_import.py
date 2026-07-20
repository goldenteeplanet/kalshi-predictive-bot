from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.runtime_export_import import (
    write_runtime_export_import_preview,
)


parser = argparse.ArgumentParser(description="Run local PMB-34C user-owned export preview")
parser.add_argument(
    "--manifest", type=Path,
    default=Path("tests/fixtures/pmb34c/manifest.json"),
)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb34c"))
args = parser.parse_args()
print(write_runtime_export_import_preview(args.manifest, args.output_dir))
