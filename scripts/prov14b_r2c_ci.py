"""Run the one-command offline PROV-14B R2B-to-R2A CI gate."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from kalshi_predictor.phase_prov14b_r2c import (
    run_capture_certification_pipeline,
    write_pipeline_report,
)

parser = argparse.ArgumentParser(description=__doc__)
for name in (
    "backup",
    "writer-monitor",
    "locks",
    "services",
    "execution",
    "cycle",
    "attribution",
):
    parser.add_argument(f"--{name}", type=Path, required=True)
parser.add_argument("--rollback-root", type=Path, required=True)
parser.add_argument("--rollback-path", action="append", required=True)
parser.add_argument("--captured-at", type=datetime.fromisoformat, required=True)
parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
parser.add_argument("--synthetic-preview", action="store_true")
parser.add_argument(
    "--output",
    type=Path,
    default=Path("reports/phase_prov14b_r2c/prov14b_r2c_ci_report.json"),
)
args = parser.parse_args()
capture_kwargs = {
    "backup_path": args.backup,
    "writer_monitor_path": args.writer_monitor,
    "locks_path": args.locks,
    "services_path": args.services,
    "execution_path": args.execution,
    "cycle_path": args.cycle,
    "attribution_path": args.attribution,
    "rollback_root": args.rollback_root,
    "rollback_paths": args.rollback_path,
    "captured_at": args.captured_at,
}
report = run_capture_certification_pipeline(
    capture_kwargs=capture_kwargs,
    rollback_root=args.rollback_root,
    as_of=args.as_of,
    synthetic_preview=args.synthetic_preview,
)
print(write_pipeline_report(report, args.output))
raise SystemExit(report["summary"]["ci_exit_code"])
