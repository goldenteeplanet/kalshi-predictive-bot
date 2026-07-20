"""Generate GH-1X from a completed GH-1V report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.phase_gh1x import build_gh1x_census, write_report

parser = argparse.ArgumentParser()
parser.add_argument(
    "--source",
    type=Path,
    default=Path("reports/phase_gh1v/gh1v_fresh_near_miss_multi_window_watch.json"),
)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_gh1x"))
args = parser.parse_args()
source = json.loads(args.source.read_text(encoding="utf-8"))
report = build_gh1x_census(source)
path = write_report(report, args.output_dir)
print(path)
raise SystemExit(0 if report["status"] == "PASSED" else 2)
