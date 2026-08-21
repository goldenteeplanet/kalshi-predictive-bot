#!/usr/bin/env python3
"""Rerun the frozen pessimistic comparison once its settled cohort is valid."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def should_rerun(status: dict, state: dict) -> bool:
    threshold = int(status.get("minimum_settled_interval_cohort", 10))
    settled = int(status.get("settled_interval_eligible_events", 0))
    completed = int(state.get("last_successful_threshold", 0))
    return settled >= threshold and completed < threshold


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--alignment-manifest", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    status = json.loads(args.status.read_text(encoding="utf-8"))
    state = (
        json.loads(args.state.read_text(encoding="utf-8"))
        if args.state.exists()
        else {}
    )
    if not should_rerun(status, state):
        print(json.dumps({"rerun": False, "reason": "COHORT_THRESHOLD_NOT_CROSSED"}))
        return 0

    command = [
        sys.executable,
        str(Path(__file__).with_name("crypto_event_multiclass_walk_forward.py")),
        "--database", str(args.database),
        "--output", str(args.output),
        "--liquidity-policy", str(args.status),
        "--alignment-manifest", str(args.alignment_manifest),
    ]
    completed = subprocess.run(command, check=False, timeout=args.timeout_seconds)
    if completed.returncode != 0:
        print(json.dumps({"rerun": True, "success": False, "returncode": completed.returncode}))
        return completed.returncode

    threshold = int(status.get("minimum_settled_interval_cohort", 10))
    state.update(
        {
            "last_successful_threshold": threshold,
            "settled_events_at_rerun": int(
                status.get("settled_interval_eligible_events", 0)
            ),
            "completed_at": datetime.now(UTC).isoformat(),
            "output": str(args.output),
        }
    )
    args.state.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.state.with_suffix(args.state.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.state)
    print(json.dumps({"rerun": True, "success": True, **state}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
