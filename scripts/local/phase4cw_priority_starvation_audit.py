"""Audit bounded service fairness and stale-state visibility from schedule traces."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cw.schedule-trace.v1"
REPORT_SCHEMA = "phase4cw.starvation-audit.v1"
MAX_MARKETS = 100_000
MAX_CYCLES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {"schema", "markets", "max_wait_cycles", "cycles", "artifact_hash"}
    if set(payload) != fields:
        raise ValueError("PHASE4CW_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CW_INPUT_SCHEMA_OR_HASH_INVALID")
    markets = payload.get("markets")
    if (
        not isinstance(markets, list)
        or not markets
        or len(markets) > MAX_MARKETS
        or any(not isinstance(market, str) or not market for market in markets)
        or len(set(markets)) != len(markets)
    ):
        raise ValueError("PHASE4CW_MARKETS_INVALID")
    market_set = set(markets)
    max_wait = payload.get("max_wait_cycles")
    if isinstance(max_wait, bool) or not isinstance(max_wait, int) or max_wait <= 0:
        raise ValueError("PHASE4CW_MAX_WAIT_INVALID")
    cycles = payload.get("cycles")
    if not isinstance(cycles, list) or not cycles or len(cycles) > MAX_CYCLES:
        raise ValueError("PHASE4CW_CYCLE_COUNT_INVALID")

    served_by_cycle: list[set[str]] = []
    stale_visibility_violations = []
    for expected_cycle, cycle in enumerate(cycles, start=1):
        required = {"cycle", "served_markets", "stale_markets", "surfaced_stale_markets"}
        if not isinstance(cycle, dict) or set(cycle) != required:
            raise ValueError("PHASE4CW_CYCLE_FIELDS_INVALID")
        if cycle["cycle"] != expected_cycle:
            raise ValueError("PHASE4CW_CYCLE_SEQUENCE_INVALID")
        collections = {}
        for field in ("served_markets", "stale_markets", "surfaced_stale_markets"):
            values = cycle[field]
            if (
                not isinstance(values, list)
                or len(set(values)) != len(values)
                or any(value not in market_set for value in values)
            ):
                raise ValueError("PHASE4CW_CYCLE_MARKETS_INVALID")
            collections[field] = set(values)
        served_by_cycle.append(collections["served_markets"])
        hidden = sorted(collections["stale_markets"] - collections["surfaced_stale_markets"])
        for ticker in hidden:
            stale_visibility_violations.append({"cycle": expected_cycle, "ticker": ticker})

    market_results = []
    window_size = min(max_wait, len(cycles))
    for ticker in markets:
        violating_windows = []
        for start in range(0, len(cycles) - window_size + 1):
            indices = range(start, start + window_size)
            if not any(ticker in served_by_cycle[index] for index in indices):
                violating_windows.append(
                    {"start_cycle": start + 1, "end_cycle": start + window_size}
                )
        served_cycles = [
            index + 1 for index, served in enumerate(served_by_cycle) if ticker in served
        ]
        market_results.append(
            {
                "ticker": ticker,
                "status": "PASS" if not violating_windows else "STARVED",
                "served_cycles": served_cycles,
                "violating_windows": violating_windows,
            }
        )
    starvation = sum(row["status"] == "STARVED" for row in market_results)
    passed = starvation == 0 and not stale_visibility_violations
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CW",
        "input_hash": payload["artifact_hash"],
        "status": "FAIRNESS_PROVEN" if passed else "REFUSE",
        "max_wait_cycles": max_wait,
        "audited_window_size": window_size,
        "markets": market_results,
        "starved_market_count": starvation,
        "stale_visibility_violations": stale_visibility_violations,
        "trading_actions_created": 0,
        "execution_authorized": False,
        "production_records_created": 0,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.trace.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
