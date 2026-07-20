"""Run R5-RECOVERY-8 against a completed three-cycle census."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.r5_recovery8 import consolidate_three_cycle_census, write_report

parser = argparse.ArgumentParser()
parser.add_argument("--census", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_r5_recovery8"))
args = parser.parse_args()
census = json.loads(args.census.read_text(encoding="utf-8"))
report = consolidate_three_cycle_census(census)
path = write_report(report, args.output_dir)
print(path)
raise SystemExit(0 if report["status"] == "PASSED" else 2)
