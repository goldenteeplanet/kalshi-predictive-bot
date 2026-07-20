from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.provenance.ci_gate import write_offline_ci_gate

parser = argparse.ArgumentParser(description="Run the offline PROV-15G CI gate")
parser.add_argument("--root", type=Path, default=Path("."))
parser.add_argument("--bundle", type=Path, required=True)
parser.add_argument("--manifest", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

path, exit_code = write_offline_ci_gate(
    bundle_path=args.bundle,
    manifest_path=args.manifest,
    root=args.root,
    output_path=args.output,
)
print(path)
raise SystemExit(exit_code)
