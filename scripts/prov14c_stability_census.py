from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.phase_prov14c import write_prov14c_stability_census

parser = argparse.ArgumentParser(description="Build the read-only PROV-14C stability census.")
parser.add_argument("reports", nargs="+", type=Path)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_prov14c"))
parser.add_argument("--synthetic-preview", action="store_true")
args = parser.parse_args()

print(
    write_prov14c_stability_census(
        args.reports,
        args.output_dir,
        synthetic_preview=args.synthetic_preview,
    )
)
