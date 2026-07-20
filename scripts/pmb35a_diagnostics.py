from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.benchmarking.pmb35a_diagnostics import write_pmb35a_diagnostics

parser = argparse.ArgumentParser(description="Diagnose exact PMB-35 shadow input blockers.")
parser.add_argument("--mapping", type=Path, default=Path("reports/phase_pmb34a/pmb34a_exact_shadow_field_source_mapping_preview.json"))
parser.add_argument("--output", type=Path, default=Path("reports/phase_pmb35a/pmb35a_exact_weather_reference_diagnostics.json"))
args = parser.parse_args()
print(write_pmb35a_diagnostics(args.mapping, args.output))
