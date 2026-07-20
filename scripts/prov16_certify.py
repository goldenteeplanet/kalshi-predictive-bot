"""Run the PROV-16 offline certification."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from kalshi_predictor.prov16 import certify_provenance_export, write_report

parser = argparse.ArgumentParser()
parser.add_argument("--events", type=Path, required=True)
parser.add_argument("--dashboard", type=Path, required=True)
parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
parser.add_argument("--retention-days", type=int, default=30)
parser.add_argument("--latency-limit-ms", type=float, default=50.0)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_prov16"))
args = parser.parse_args()
report = certify_provenance_export(
    events_path=args.events,
    dashboard_path=args.dashboard,
    as_of=args.as_of,
    retention_days=args.retention_days,
    query_latency_limit_ms=args.latency_limit_ms,
)
path = write_report(report, args.output_dir)
print(path)
raise SystemExit(0 if report["status"] == "PASSED" else 2)
