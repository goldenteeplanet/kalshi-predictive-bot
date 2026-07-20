"""Build a PROV-14B-R2A bundle from user-owned JSON evidence."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from kalshi_predictor.phase_prov14b_r2a import build_certification_bundle, write_bundle


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


parser = argparse.ArgumentParser(description=__doc__)
for name in ("backup", "rollback", "safety", "cycle", "attribution"):
    parser.add_argument(f"--{name}", type=Path, required=True)
parser.add_argument("--rollback-root", type=Path, required=True)
parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
parser.add_argument(
    "--output",
    type=Path,
    default=Path("reports/phase_prov14b_r2a/prov14b_r2a_certification_bundle.json"),
)
args = parser.parse_args()
report = build_certification_bundle(
    backup=_json(args.backup),
    rollback=_json(args.rollback),
    safety=_json(args.safety),
    cycle=_json(args.cycle),
    attribution=_json(args.attribution),
    rollback_root=args.rollback_root,
    as_of=args.as_of,
)
print(write_bundle(report, args.output))
raise SystemExit(0 if report["status"] == "PASSED" else 2)
