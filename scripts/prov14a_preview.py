from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.phase_prov14a import write_prov14a_repair_preview

parser = argparse.ArgumentParser(description="Generate local no-write PROV-14A repair preview")
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_prov14a"))
args = parser.parse_args()
print(write_prov14a_repair_preview(args.output_dir))
