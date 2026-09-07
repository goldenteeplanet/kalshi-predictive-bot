"""Build a deterministic circuit-breaker evidence report from offline observations."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ci.circuit-evidence.v1"
REPORT_SCHEMA = "phase4ci.circuit-report.v1"
API_OUTCOMES = ("OK", "THROTTLED", "TIMEOUT", "UNAVAILABLE")
QUALITY_OUTCOMES = ("VALID", "INVALID", "UNKNOWN")
MAX_THRESHOLD = 100


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4CI_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4CI_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC or parsed.microsecond:
        raise ValueError("PHASE4CI_TIMESTAMP_INVALID")
    return parsed


def _threshold(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_THRESHOLD:
        raise ValueError(f"PHASE4CI_{name.upper()}_INVALID")
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {
        "schema",
        "api_open_threshold",
        "quality_open_threshold",
        "recovery_threshold",
        "observations",
        "artifact_hash",
    }:
        raise ValueError("PHASE4CI_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CI_INPUT_SCHEMA_OR_HASH_INVALID")
    api_limit = _threshold(payload, "api_open_threshold")
    quality_limit = _threshold(payload, "quality_open_threshold")
    recovery_limit = _threshold(payload, "recovery_threshold")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("PHASE4CI_OBSERVATIONS_MISSING")

    previous: datetime | None = None
    api_failures = quality_failures = recoveries = 0
    state = "CLOSED"
    transitions: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for sequence, observation in enumerate(observations, start=1):
        if not isinstance(observation, dict) or set(observation) != {
            "observed_at",
            "api_outcome",
            "quality_outcome",
        }:
            raise ValueError("PHASE4CI_OBSERVATION_FIELDS_INVALID")
        observed_at = _timestamp(observation["observed_at"])
        if previous is not None and observed_at <= previous:
            raise ValueError("PHASE4CI_OBSERVATIONS_NOT_CHRONOLOGICAL")
        previous = observed_at
        api = observation["api_outcome"]
        quality = observation["quality_outcome"]
        if api not in API_OUTCOMES or quality not in QUALITY_OUTCOMES:
            raise ValueError("PHASE4CI_OUTCOME_INVALID")

        api_failures = 0 if api == "OK" else api_failures + 1
        quality_failures = quality_failures + 1 if quality == "INVALID" else 0
        if api == "OK" and quality == "VALID":
            recoveries += 1
        else:
            recoveries = 0

        prior = state
        reason = "NO_TRANSITION"
        if quality_failures >= quality_limit:
            state, reason = "OPEN_DATA_QUALITY", "PERSISTENT_INVALID_DATA"
        elif api_failures >= api_limit:
            state, reason = "OPEN_API", "PERSISTENT_API_FAILURE"
        elif api_failures:
            state, reason = "DEGRADED_API", "TRANSIENT_API_FAILURE"
        elif state != "CLOSED" and recoveries < recovery_limit:
            state, reason = "RECOVERING", "RECOVERY_EVIDENCE_INCOMPLETE"
        elif recoveries >= recovery_limit:
            state, reason = "CLOSED", "RECOVERY_THRESHOLD_MET"
        else:
            state = "CLOSED"
        if state != prior:
            transitions.append(
                {
                    "sequence": sequence,
                    "observed_at": observation["observed_at"],
                    "from": prior,
                    "to": state,
                    "reason": reason,
                }
            )
        evidence.append(
            {
                "sequence": sequence,
                "observed_at": observation["observed_at"],
                "state": state,
                "reason": reason,
                "api_failure_run": api_failures,
                "quality_failure_run": quality_failures,
                "recovery_run": recoveries,
            }
        )

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CI",
        "input_hash": payload["artifact_hash"],
        "final_state": state,
        "execution_authorized": False,
        "production_records_created": 0,
        "observations": evidence,
        "transitions": transitions,
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
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.evidence.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
