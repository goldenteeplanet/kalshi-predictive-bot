#!/usr/bin/env python3
"""Merge the latest targeted cycle into persistent event-level telemetry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.research.targeted_forecast_telemetry import (
    update_history,
    write_history,
)


def _read(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = update_history(
        _read(args.output), _read(args.collector), _read(args.alignment)
    )
    write_history(args.output, payload)
    print(json.dumps({key: value for key, value in payload.items() if key != "observations"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
