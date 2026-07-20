from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.prov14c_import import write_import_bundle

parser = argparse.ArgumentParser(description="Import bounded runtime provenance exports read-only.")
parser.add_argument("exports", nargs="+", type=Path)
parser.add_argument("--output", type=Path, default=Path("reports/phase_prov14ca/prov14ca_runtime_export_import.json"))
args = parser.parse_args()
print(write_import_bundle(args.exports, args.output))
