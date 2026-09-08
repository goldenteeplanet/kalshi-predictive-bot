"""Audit supplied weather-source timing and reconciliation evidence offline."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cm.weather-latency-input.v1"
REPORT_SCHEMA = "phase4cm.weather-latency-report.v1"
TIMESTAMP_MEANINGS = ("OBSERVATION_TIME", "ISSUE_TIME", "VALID_TIME")
MAX_SAMPLES = 10_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4CM_TIMESTAMP_INVALID")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4CM_TIMESTAMP_INVALID") from exc
    if result.tzinfo != UTC:
        raise ValueError("PHASE4CM_TIMESTAMP_INVALID")
    return result


def _ms(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _bound(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"PHASE4CM_{name.upper()}_INVALID")
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema",
        "evaluated_at",
        "max_latency_ms",
        "max_freshness_age_ms",
        "reconciliation_tolerance",
        "samples",
        "artifact_hash",
    }
    if set(payload) != fields:
        raise ValueError("PHASE4CM_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CM_INPUT_SCHEMA_OR_HASH_INVALID")
    evaluated_at = _time(payload["evaluated_at"])
    max_latency = _bound(payload, "max_latency_ms")
    max_age = _bound(payload, "max_freshness_age_ms")
    try:
        tolerance = Decimal(str(payload["reconciliation_tolerance"]))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("PHASE4CM_TOLERANCE_INVALID") from exc
    if not tolerance.is_finite() or tolerance < 0:
        raise ValueError("PHASE4CM_TOLERANCE_INVALID")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples or len(samples) > MAX_SAMPLES:
        raise ValueError("PHASE4CM_SAMPLE_COUNT_INVALID")

    identifiers: set[str] = set()
    groups: dict[str, list[dict[str, Any]]] = {}
    rows = []
    for sample in samples:
        required = {
            "sample_id",
            "reconciliation_key",
            "source",
            "available",
            "requested_at",
            "received_at",
            "data_timestamp",
            "timestamp_meaning",
            "value",
        }
        if not isinstance(sample, dict) or set(sample) != required:
            raise ValueError("PHASE4CM_SAMPLE_FIELDS_INVALID")
        identifier = sample["sample_id"]
        source = sample["source"]
        key = sample["reconciliation_key"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4CM_SAMPLE_ID_INVALID")
        if not isinstance(source, str) or not source or not isinstance(key, str) or not key:
            raise ValueError("PHASE4CM_SOURCE_OR_KEY_INVALID")
        if not isinstance(sample["available"], bool):
            raise ValueError("PHASE4CM_AVAILABILITY_INVALID")
        meaning = sample["timestamp_meaning"]
        if meaning not in TIMESTAMP_MEANINGS:
            raise ValueError("PHASE4CM_TIMESTAMP_MEANING_INVALID")
        requested = _time(sample["requested_at"])
        received = _time(sample["received_at"])
        data_time = _time(sample["data_timestamp"])
        latency = _ms(received, requested)
        age = _ms(evaluated_at, data_time)
        if latency < 0 or age < 0:
            raise ValueError("PHASE4CM_TEMPORAL_ORDER_INVALID")
        try:
            value = Decimal(str(sample["value"]))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("PHASE4CM_VALUE_INVALID") from exc
        if not value.is_finite():
            raise ValueError("PHASE4CM_VALUE_INVALID")
        identifiers.add(identifier)
        row = {
            "sample_id": identifier,
            "source": source,
            "reconciliation_key": key,
            "available": sample["available"],
            "latency_ms": latency,
            "freshness_age_ms": age,
            "latency_status": "PASS" if latency <= max_latency else "SLOW",
            "freshness_status": "PASS" if age <= max_age else "STALE",
            "timestamp_meaning": meaning,
            "value": str(value),
        }
        rows.append(row)
        groups.setdefault(key, []).append(row)

    reconciliations = []
    for key in sorted(groups):
        group = [row for row in groups[key] if row["available"]]
        meanings = sorted({row["timestamp_meaning"] for row in group})
        if len(group) < 2:
            status, spread = "INSUFFICIENT_AVAILABLE_SOURCES", None
        elif len(meanings) != 1:
            status, spread = "INCOMPATIBLE_TIMESTAMP_MEANING", None
        else:
            values = [Decimal(row["value"]) for row in group]
            spread_value = max(values) - min(values)
            spread = str(spread_value)
            status = "PASS" if spread_value <= tolerance else "DIVERGENT"
        reconciliations.append(
            {
                "reconciliation_key": key,
                "status": status,
                "available_source_count": len(group),
                "timestamp_meanings": meanings,
                "spread": spread,
            }
        )

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CM",
        "input_hash": payload["artifact_hash"],
        "sample_count": len(rows),
        "available_count": sum(row["available"] for row in rows),
        "samples": rows,
        "reconciliations": reconciliations,
        "network_calls_performed": 0,
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
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.samples.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
