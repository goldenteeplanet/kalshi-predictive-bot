"""Generate PMB-36 from multiple disabled shadow reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.benchmarking.shadow_census import build_shadow_census, write_report

parser = argparse.ArgumentParser()
parser.add_argument("reports", nargs="+", type=Path)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_pmb36"))
args = parser.parse_args()
cycles = []
for path in args.reports:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("cycle_id", path.stem)
    cycles.append(payload)
report = build_shadow_census(cycles)
path = write_report(report, args.output_dir)
print(path)
raise SystemExit(0 if report["status"] == "PASSED" else 2)
