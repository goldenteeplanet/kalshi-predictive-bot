from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.phase_prov14b_r4 import write_prov14b_r4_preview

parser = argparse.ArgumentParser(description="Write the local no-write PROV-14B-R4 preview")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_prov14b_r4"))
args = parser.parse_args()

print(write_prov14b_r4_preview(args.output_dir))
