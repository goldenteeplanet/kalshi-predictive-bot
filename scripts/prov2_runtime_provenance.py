"""Command-line entry point for the PROV-2 read-only runtime audit."""

from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.runtime_provenance import write_runtime_provenance_audit


parser = argparse.ArgumentParser()
parser.add_argument("--database", type=Path, default=Path("data/kalshi_phase1.db"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_prov2"))
parser.add_argument("--models", default="crypto_v2,weather_v2,sports_v1")
parser.add_argument("--max-rows", type=int, default=100)
args = parser.parse_args()
models = [value.strip() for value in args.models.split(",") if value.strip()]
print(write_runtime_provenance_audit(
    database_path=args.database, output_dir=args.output_dir,
    model_names=models, max_rows=args.max_rows,
))
