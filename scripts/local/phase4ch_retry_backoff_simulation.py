"""Simulate deterministic retry and backoff behavior without connected I/O or sleeping."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ch.retry-scenarios.v1"
REPORT_SCHEMA = "phase4ch.retry-simulation.v1"
OUTCOMES = ("SUCCESS", "THROTTLED", "TIMEOUT", "PARTIAL_PAGE", "MALFORMED")
MAX_RETRIES = 8
MAX_BACKOFF_MS = 60_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def build_simulation(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CH_INPUT_SCHEMA_OR_HASH_INVALID")
    retry_limit = payload.get("retry_limit")
    base_backoff = payload.get("base_backoff_ms")
    scenarios = payload.get("scenarios")
    if (
        isinstance(retry_limit, bool)
        or not isinstance(retry_limit, int)
        or not 0 <= retry_limit <= MAX_RETRIES
    ):
        raise ValueError("PHASE4CH_RETRY_LIMIT_INVALID")
    if isinstance(base_backoff, bool) or not isinstance(base_backoff, int) or base_backoff <= 0:
        raise ValueError("PHASE4CH_BASE_BACKOFF_INVALID")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("PHASE4CH_SCENARIOS_MISSING")
    names: set[str] = set()
    results = []
    for scenario in scenarios:
        fields = {"name", "outcomes", "retry_after_ms", "partial_page_idempotent"}
        if not isinstance(scenario, dict) or set(scenario) != fields:
            raise ValueError("PHASE4CH_SCENARIO_FIELDS_INVALID")
        name = scenario["name"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("PHASE4CH_SCENARIO_NAME_INVALID")
        names.add(name)
        outcomes = scenario["outcomes"]
        retry_after = scenario["retry_after_ms"]
        idempotent = scenario["partial_page_idempotent"]
        if (
            not isinstance(outcomes, list)
            or not outcomes
            or any(value not in OUTCOMES for value in outcomes)
        ):
            raise ValueError("PHASE4CH_OUTCOMES_INVALID")
        if not isinstance(retry_after, list) or len(retry_after) != len(outcomes):
            raise ValueError("PHASE4CH_RETRY_AFTER_SHAPE_INVALID")
        if not isinstance(idempotent, bool):
            raise ValueError("PHASE4CH_IDEMPOTENCY_INVALID")
        for value in retry_after:
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError("PHASE4CH_RETRY_AFTER_VALUE_INVALID")
        attempts = []
        status = "RETRY_EXHAUSTED"
        maximum_attempts = retry_limit + 1
        for attempt, outcome in enumerate(outcomes[:maximum_attempts], start=1):
            delay = 0
            terminal = False
            if outcome == "SUCCESS":
                status, terminal = "SUCCESS", True
            elif outcome == "MALFORMED":
                status, terminal = "MALFORMED_TERMINAL", True
            elif outcome == "PARTIAL_PAGE" and not idempotent:
                status, terminal = "UNSAFE_PARTIAL_PAGE", True
            elif attempt < maximum_attempts:
                exponential = min(MAX_BACKOFF_MS, base_backoff * (2 ** (attempt - 1)))
                header_delay = retry_after[attempt - 1] or 0
                delay = min(MAX_BACKOFF_MS, max(exponential, header_delay))
            attempts.append(
                {"attempt": attempt, "outcome": outcome, "backoff_before_next_ms": delay}
            )
            if terminal:
                break
        results.append(
            {
                "name": name,
                "status": status,
                "attempts": attempts,
                "attempt_count": len(attempts),
                "total_backoff_ms": sum(row["backoff_before_next_ms"] for row in attempts),
            }
        )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CH",
        "input_hash": payload["artifact_hash"],
        "retry_limit": retry_limit,
        "base_backoff_ms": base_backoff,
        "scenarios": results,
        "deterministic": True,
        "sleep_calls": 0,
        "live_setting_changes_applied": 0,
        "production_records_created": 0,
        "execution_authorized": False,
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
    parser.add_argument("--scenarios", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_simulation(json.loads(args.scenarios.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
