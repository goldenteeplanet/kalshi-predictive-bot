from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.offline_export_join import (
    write_offline_exact_export_join_preview,
)


parser = argparse.ArgumentParser(description="Run local PMB-34B exact export join preview")
parser.add_argument(
    "--fixtures", type=Path,
    default=Path("tests/fixtures/pmb34b/offline_runtime_exports.json"),
)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb34b"))
args = parser.parse_args()
print(write_offline_exact_export_join_preview(args.fixtures, args.output_dir))
