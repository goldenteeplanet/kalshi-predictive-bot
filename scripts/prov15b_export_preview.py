from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.provenance.export_adapter import (
    load_runtime_provenance_export,
    write_runtime_export_comparison,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

parser = argparse.ArgumentParser(description="PROV-15B read-only export comparison")
parser.add_argument("--input", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--expected", default="crypto_v2:2.0.0,weather_v2:2.0.0")
parser.add_argument("--generated-at")
parser.add_argument("--max-rows", type=int, default=200)
args = parser.parse_args()

expected: dict[str, list[str]] = {}
for item in args.expected.split(","):
    model, separator, version = item.strip().partition(":")
    if not separator or not model or not version:
        raise SystemExit(f"invalid --expected entry: {item!r}")
    expected.setdefault(model, []).append(version)
generated_at = parse_datetime(args.generated_at) if args.generated_at else utc_now()
if generated_at is None:
    raise SystemExit("--generated-at must be an ISO-8601 timestamp")

payload = load_runtime_provenance_export(args.input)
print(write_runtime_export_comparison(
    payload,
    expected_model_versions=expected,
    generated_at=generated_at,
    output_path=args.output,
    max_rows=args.max_rows,
))
