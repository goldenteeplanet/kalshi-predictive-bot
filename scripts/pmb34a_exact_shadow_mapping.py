from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.exact_shadow_mapping import (
    write_exact_shadow_field_mapping_preview,
)


parser = argparse.ArgumentParser(description="Run local PMB-34A exact source mapping preview")
parser.add_argument(
    "--fixtures",
    type=Path,
    default=Path("tests/fixtures/pmb34a/exact_shadow_source_exports.json"),
)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb34a"))
args = parser.parse_args()
print(write_exact_shadow_field_mapping_preview(args.fixtures, args.output_dir))
