#!/usr/bin/env python3
"""Atomically update the active fixed-rate scheduler health artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.research.fixed_rate_health import update_health, write_health


def _read(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stage", "finish"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycle-id")
    parser.add_argument("--cycle-started-at")
    parser.add_argument("--stage")
    parser.add_argument("--stage-started-at")
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--coinbase-report", type=Path)
    parser.add_argument("--gh2-report", type=Path)
    parser.add_argument("--cadence-seconds", type=int, default=900)
    args = parser.parse_args()
    payload = update_health(
        _read(args.output),
        action=args.action,
        cycle_id=args.cycle_id,
        cycle_started_at=args.cycle_started_at,
        stage=args.stage,
        stage_started_at=args.stage_started_at,
        exit_code=args.exit_code,
        timeout_seconds=args.timeout_seconds,
        coinbase_report=_read(args.coinbase_report),
        gh2_report=_read(args.gh2_report),
        cadence_seconds=args.cadence_seconds,
    )
    write_health(args.output, payload)
    print(json.dumps({key: value for key, value in payload.items() if key != "stages"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
