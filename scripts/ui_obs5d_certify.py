from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.ui.cloud_snapshot_parity import certify_cloud_snapshot_parity


parser = argparse.ArgumentParser(description="Certify captured cloud dashboard snapshot parity without deployment.")
parser.add_argument("--snapshot", type=Path, required=True)
parser.add_argument("--authoritative", type=Path, required=True)
parser.add_argument("--output", type=Path, default=Path("reports/phase_ui_obs5d/ui_obs5d_cloud_snapshot_parity.json"))
parser.add_argument("--reference-time")
args = parser.parse_args()

snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
authoritative = json.loads(args.authoritative.read_text(encoding="utf-8"))
reference = datetime.fromisoformat(args.reference_time.replace("Z", "+00:00")).astimezone(UTC) if args.reference_time else datetime.now(UTC)
report = certify_cloud_snapshot_parity(snapshot, authoritative, reference_time=reference)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(args.output)
