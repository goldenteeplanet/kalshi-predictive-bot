from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.provenance.triage import write_offline_provenance_triage

parser = argparse.ArgumentParser(description="Generate offline PROV-15D failure triage")
parser.add_argument("--input", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

payload = json.loads(args.input.read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("input report must be a JSON object")
print(write_offline_provenance_triage(payload, args.output))
