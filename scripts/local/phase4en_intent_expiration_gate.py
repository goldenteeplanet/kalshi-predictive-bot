"""Reject intents whose evidence or approval expires before safe routing can finish."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4en.expiration-input.v1"
REPORT_SCHEMA = "phase4en.expiration-report.v1"
EXPIRATIONS = (
    ("intent_expires_at", "INTENT_EXPIRES_BEFORE_ROUTING"),
    ("evidence_expires_at", "EVIDENCE_EXPIRES_BEFORE_ROUTING"),
    ("approval_expires_at", "APPROVAL_EXPIRES_BEFORE_ROUTING"),
)


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4EN_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4EN_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("PHASE4EN_TIMESTAMP_INVALID")
    return parsed


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _milliseconds(delta: Any) -> int:
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _nonnegative_int(value: Any, error: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(error)
    return value


def _digest(value: Any) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4EN_HANDOFF_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4EN_HANDOFF_HASH_INVALID") from exc


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "as_of",
        "routing_duration_ms",
        "safety_margin_ms",
        "intents",
        "artifact_hash",
    }
    if set(payload) != required:
        raise ValueError("PHASE4EN_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EN_INPUT_SCHEMA_OR_HASH_INVALID")
    as_of = _timestamp(payload["as_of"])
    duration = _nonnegative_int(payload["routing_duration_ms"], "PHASE4EN_DURATION_INVALID")
    margin = _nonnegative_int(payload["safety_margin_ms"], "PHASE4EN_MARGIN_INVALID")
    completion = as_of + timedelta(milliseconds=duration + margin)
    intents = payload.get("intents")
    if not isinstance(intents, list) or not intents:
        raise ValueError("PHASE4EN_INTENTS_EMPTY")
    seen = set()
    results = []
    fields = {"intent_id", "handoff_artifact_hash", *(field for field, _ in EXPIRATIONS)}
    for intent in intents:
        if not isinstance(intent, dict) or set(intent) != fields:
            raise ValueError("PHASE4EN_INTENT_FIELDS_INVALID")
        identifier = intent["intent_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("PHASE4EN_INTENT_ID_INVALID")
        seen.add(identifier)
        _digest(intent["handoff_artifact_hash"])
        reasons = []
        headroom = {}
        for field, reason in EXPIRATIONS:
            expiration = _timestamp(intent[field])
            remaining = _milliseconds(expiration - completion)
            headroom[field.removesuffix("_at") + "_headroom_ms"] = remaining
            if remaining < 0:
                reasons.append(reason)
        results.append(
            {
                "intent_id": identifier,
                "handoff_artifact_hash": intent["handoff_artifact_hash"],
                "status": "ELIGIBLE_FOR_SIMULATED_ROUTING" if not reasons else "REFUSE",
                "reasons": reasons,
                **headroom,
            }
        )
    results.sort(key=lambda row: row["intent_id"])
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EN",
        "input_hash": payload["artifact_hash"],
        "as_of": payload["as_of"],
        "routing_duration_ms": duration,
        "safety_margin_ms": margin,
        "required_completion_at": _format_timestamp(completion),
        "results": results,
        "eligible_count": sum(row["status"] == "ELIGIBLE_FOR_SIMULATED_ROUTING" for row in results),
        "refused_count": sum(row["status"] == "REFUSE" for row in results),
        "routing_attempted": False,
        "paper_orders_created": 0,
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
