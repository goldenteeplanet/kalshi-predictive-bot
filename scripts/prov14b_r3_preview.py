from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.phase_prov14b_r3 import write_prov14b_r3_preview

parser = argparse.ArgumentParser(description="Write the local no-write PROV-14B-R3 preview")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_prov14b_r3"))
args = parser.parse_args()

print(write_prov14b_r3_preview(args.output_dir))
