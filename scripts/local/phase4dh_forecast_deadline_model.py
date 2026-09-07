"""Assign deterministic forecast compute deadlines from supplied timing budgets."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dh.deadline-input.v1"
REPORT_SCHEMA = "phase4dh.deadline-report.v1"
MAX_FORECASTS = 100_000
MAX_SECONDS = 31_536_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DH_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DH_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DH_TIMESTAMP_INVALID")
    return parsed


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _seconds(value: Any, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= MAX_SECONDS:
        raise ValueError("PHASE4DH_DURATION_INVALID")
    return value


def _microseconds(value: timedelta) -> int:
    return (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "evaluated_at", "forecasts", "artifact_hash"}:
        raise ValueError("PHASE4DH_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DH_INPUT_SCHEMA_OR_HASH_INVALID")
    evaluated_at = _time(payload["evaluated_at"])
    forecasts = payload["forecasts"]
    if not isinstance(forecasts, list) or not forecasts or len(forecasts) > MAX_FORECASTS:
        raise ValueError("PHASE4DH_FORECAST_COUNT_INVALID")
    fields = {
        "forecast_id",
        "market_id",
        "evidence_observed_at",
        "freshness_window_seconds",
        "market_close_at",
        "ranking_budget_seconds",
        "risk_budget_seconds",
        "publication_buffer_seconds",
    }
    identifiers: set[str] = set()
    assignments = []
    for forecast in forecasts:
        if not isinstance(forecast, dict) or set(forecast) != fields:
            raise ValueError("PHASE4DH_FORECAST_FIELDS_INVALID")
        identifier, market = forecast["forecast_id"], forecast["market_id"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
            or not isinstance(market, str)
            or not market
        ):
            raise ValueError("PHASE4DH_FORECAST_IDENTITY_INVALID")
        identifiers.add(identifier)
        observed = _time(forecast["evidence_observed_at"])
        market_close = _time(forecast["market_close_at"])
        if observed > evaluated_at or market_close <= evaluated_at:
            raise ValueError("PHASE4DH_TIMELINE_INVALID")
        freshness = _seconds(forecast["freshness_window_seconds"], positive=True)
        ranking = _seconds(forecast["ranking_budget_seconds"])
        risk = _seconds(forecast["risk_budget_seconds"])
        publication = _seconds(forecast["publication_buffer_seconds"])
        freshness_deadline = observed + timedelta(seconds=freshness)
        close_deadline = market_close - timedelta(seconds=ranking + risk + publication)
        compute_deadline = min(freshness_deadline, close_deadline)
        limiting = (
            "BOTH"
            if freshness_deadline == close_deadline
            else "EVIDENCE_FRESHNESS"
            if freshness_deadline < close_deadline
            else "MARKET_CLOSE_RESERVES"
        )
        remaining_microseconds = _microseconds(compute_deadline - evaluated_at)
        assignments.append(
            {
                "forecast_id": identifier,
                "market_id": market,
                "freshness_deadline": _timestamp(freshness_deadline),
                "market_close_reserve_deadline": _timestamp(close_deadline),
                "compute_deadline": _timestamp(compute_deadline),
                "limiting_constraint": limiting,
                "remaining_compute_microseconds": remaining_microseconds,
                "deadline_already_elapsed": remaining_microseconds < 0,
            }
        )
    assignments.sort(key=lambda row: (row["compute_deadline"], row["forecast_id"]))
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DH",
        "input_hash": payload["artifact_hash"],
        "evaluated_at": payload["evaluated_at"],
        "assignments": assignments,
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
