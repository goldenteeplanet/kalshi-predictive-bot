from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.provenance.remediation import write_exact_remediation_simulation
from kalshi_predictor.utils.time import parse_datetime

parser = argparse.ArgumentParser(description="Run offline PROV-15E remediation simulation")
parser.add_argument("--spec", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

spec = json.loads(args.spec.read_text(encoding="utf-8"))
generated_at = parse_datetime(spec.get("generated_at"))
if generated_at is None:
    raise SystemExit("spec generated_at must be an ISO-8601 timestamp")
print(write_exact_remediation_simulation(
    spec.get("events", []),
    repairs_by_event_key=spec.get("repairs_by_event_key", {}),
    expected_model_versions=spec.get("expected_model_versions", {}),
    generated_at=generated_at,
    output_path=args.output,
    thresholds=spec.get("thresholds"),
))
