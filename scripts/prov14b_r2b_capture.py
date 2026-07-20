"""Normalize bounded local runtime exports for PROV-14B-R2A."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from kalshi_predictor.phase_prov14b_r2b import capture_runtime_evidence, write_capture

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
parser.add_argument(
    "--output",
    type=Path,
    default=Path("reports/phase_prov14b_r2b/prov14b_r2b_runtime_evidence_capture.json"),
)
args = parser.parse_args()
report = capture_runtime_evidence(
    backup_path=args.backup,
    writer_monitor_path=args.writer_monitor,
    locks_path=args.locks,
    services_path=args.services,
    execution_path=args.execution,
    cycle_path=args.cycle,
    attribution_path=args.attribution,
    rollback_root=args.rollback_root,
    rollback_paths=args.rollback_path,
    captured_at=args.captured_at,
)
print(write_capture(report, args.output))
raise SystemExit(0 if report["status"] == "PASSED" else 2)
