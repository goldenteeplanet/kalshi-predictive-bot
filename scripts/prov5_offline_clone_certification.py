"""Run PROV-5 only against a disposable local SQLite clone."""

import argparse
from pathlib import Path

from kalshi_predictor.phase_prov5 import write_prov5_certification


parser = argparse.ArgumentParser()
parser.add_argument("--prov3-report", type=Path,
                    default=Path("reports/phase_prov3/prov3_exact_attribution_schema_repair_preview.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_prov5"))
parser.add_argument("--volume-rows", type=int, default=5000)
args = parser.parse_args()
print(write_prov5_certification(
    prov3_report=args.prov3_report, output_dir=args.output_dir,
    volume_rows=args.volume_rows,
))
