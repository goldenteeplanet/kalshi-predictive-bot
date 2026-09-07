"""Refuse offline forecast work that cannot finish before its reserved deadline."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4di.refusal-input.v1"
REPORT_SCHEMA = "phase4di.refusal-report.v1"
MAX_FORECASTS = 100_000
MAX_MICROSECONDS = 31_536_000_000_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DI_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DI_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DI_TIMESTAMP_INVALID")
    return parsed


def _duration(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_MICROSECONDS:
        raise ValueError("PHASE4DI_DURATION_INVALID")
    return value


def _micros(delta: timedelta) -> int:
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "evaluated_at", "deadline_report_hash", "forecasts", "artifact_hash"}
    if set(payload) != required:
        raise ValueError("PHASE4DI_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DI_INPUT_SCHEMA_OR_HASH_INVALID")
    evaluated_at = _time(payload["evaluated_at"])
    if (
        not isinstance(payload["deadline_report_hash"], str)
        or len(payload["deadline_report_hash"]) != 64
    ):
        raise ValueError("PHASE4DI_DEADLINE_HASH_INVALID")
    try:
        int(payload["deadline_report_hash"], 16)
    except ValueError as exc:
        raise ValueError("PHASE4DI_DEADLINE_HASH_INVALID") from exc
    forecasts = payload["forecasts"]
    if not isinstance(forecasts, list) or not forecasts or len(forecasts) > MAX_FORECASTS:
        raise ValueError("PHASE4DI_FORECAST_COUNT_INVALID")
    fields = {
        "forecast_id",
        "compute_deadline",
        "estimated_compute_microseconds",
        "uncertainty_microseconds",
        "safety_margin_microseconds",
    }
    identifiers: set[str] = set()
    decisions = []
    for forecast in forecasts:
        if not isinstance(forecast, dict) or set(forecast) != fields:
            raise ValueError("PHASE4DI_FORECAST_FIELDS_INVALID")
        identifier = forecast["forecast_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DI_FORECAST_ID_INVALID")
        identifiers.add(identifier)
        deadline = _time(forecast["compute_deadline"])
        estimate = _duration(forecast["estimated_compute_microseconds"])
        uncertainty = _duration(forecast["uncertainty_microseconds"])
        margin = _duration(forecast["safety_margin_microseconds"])
        required = estimate + uncertainty + margin
        if required > MAX_MICROSECONDS:
            raise ValueError("PHASE4DI_TOTAL_DURATION_INVALID")
        remaining = _micros(deadline - evaluated_at)
        accepted = remaining >= required
        reasons = (
            []
            if accepted
            else ["DEADLINE_ELAPSED" if remaining < 0 else "INSUFFICIENT_RESERVED_COMPUTE_TIME"]
        )
        decisions.append(
            {
                "forecast_id": identifier,
                "disposition": "ACCEPT_OFFLINE_COMPUTE" if accepted else "REFUSE_LATE_FORECAST",
                "remaining_microseconds": remaining,
                "required_microseconds": required,
                "slack_microseconds": remaining - required,
                "reasons": reasons,
            }
        )
    decisions.sort(key=lambda row: row["forecast_id"])
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DI",
        "input_hash": payload["artifact_hash"],
        "deadline_report_hash": payload["deadline_report_hash"],
        "decisions": decisions,
        "accepted_count": sum(row["disposition"] == "ACCEPT_OFFLINE_COMPUTE" for row in decisions),
        "refused_count": sum(row["disposition"] == "REFUSE_LATE_FORECAST" for row in decisions),
        "forecast_records_created": 0,
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
