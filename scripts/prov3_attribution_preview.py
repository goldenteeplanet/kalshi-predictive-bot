"""Command-line entry point for the PROV-3 no-write mapping preview."""

import argparse
from pathlib import Path

from kalshi_predictor.phase_prov3 import write_prov3_preview


parser = argparse.ArgumentParser()
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--prov2-report", type=Path,
                    default=Path("reports/phase_prov2/prov2_runtime_forecast_ranking_provenance.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_prov3"))
parser.add_argument("--max-rows", type=int, default=100)
args = parser.parse_args()
print(write_prov3_preview(
    database_path=args.database, prov2_report=args.prov2_report,
    output_dir=args.output_dir, max_rows=args.max_rows,
))
