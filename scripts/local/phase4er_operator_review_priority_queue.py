"""Build a deterministic local, non-authorizing operator review priority queue."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4er.queue-input.v1"
REPORT_SCHEMA = "phase4er.queue-report.v1"


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(code)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    return value


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(code)
    return value


def _integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(code)
    return value


def _time(value: Any, code: str) -> datetime:
    text = _text(value, code)
    if not text.endswith("Z"):
        raise ValueError(code)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(code) from exc
    if (
        parsed.tzinfo != UTC
        or parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") != text
    ):
        raise ValueError(code)
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "as_of_utc",
        "candidates",
        "artifact_hash",
    }:
        raise ValueError("PHASE4ER_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4ER_INPUT_SCHEMA_OR_HASH_INVALID")
    as_of = _time(payload["as_of_utc"], "PHASE4ER_AS_OF_INVALID")
    candidates = payload["candidates"]
    if not isinstance(candidates, list):
        raise ValueError("PHASE4ER_CANDIDATES_INVALID")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_fields = {
        "candidate_id",
        "packet_hash",
        "expires_at_utc",
        "evidence_at_utc",
        "expected_value_micros",
        "review_cost_units",
        "eligible",
        "operator_review_required",
    }
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != expected_fields:
            raise ValueError("PHASE4ER_CANDIDATE_FIELDS_INVALID")
        candidate_id = _text(candidate["candidate_id"], "PHASE4ER_CANDIDATE_ID_INVALID")
        if candidate_id in seen:
            raise ValueError("PHASE4ER_CANDIDATE_DUPLICATE")
        seen.add(candidate_id)
        packet_hash = _digest(candidate["packet_hash"], "PHASE4ER_PACKET_HASH_INVALID")
        expires = _time(candidate["expires_at_utc"], "PHASE4ER_EXPIRATION_INVALID")
        evidence = _time(candidate["evidence_at_utc"], "PHASE4ER_EVIDENCE_TIME_INVALID")
        expected_value = _integer(
            candidate["expected_value_micros"], -(10**15), 10**15, "PHASE4ER_EXPECTED_VALUE_INVALID"
        )
        review_cost = _integer(
            candidate["review_cost_units"], 0, 10**9, "PHASE4ER_REVIEW_COST_INVALID"
        )
        if not isinstance(candidate["eligible"], bool) or not isinstance(
            candidate["operator_review_required"], bool
        ):
            raise ValueError("PHASE4ER_STATUS_INVALID")
        if evidence > as_of:
            raise ValueError("PHASE4ER_FUTURE_EVIDENCE_INVALID")
        freshness_age_ms = int((as_of - evidence).total_seconds() * 1000)
        expiration_headroom_ms = int((expires - as_of).total_seconds() * 1000)
        normalized.append(
            {
                "candidate_id": candidate_id,
                "packet_hash": packet_hash,
                "expires_at_utc": candidate["expires_at_utc"],
                "expiration_headroom_ms": expiration_headroom_ms,
                "evidence_at_utc": candidate["evidence_at_utc"],
                "freshness_age_ms": freshness_age_ms,
                "expected_value_micros": expected_value,
                "review_cost_units": review_cost,
                "eligible": candidate["eligible"],
                "operator_review_required": candidate["operator_review_required"],
            }
        )

    queued = [
        item
        for item in normalized
        if item["eligible"]
        and item["operator_review_required"]
        and item["expiration_headroom_ms"] > 0
    ]
    queued.sort(
        key=lambda item: (
            item["expires_at_utc"],
            -item["expected_value_micros"],
            item["freshness_age_ms"],
            item["review_cost_units"],
            item["candidate_id"],
        )
    )
    queue = [
        {
            "position": position,
            **{
                key: value
                for key, value in item.items()
                if key not in {"eligible", "operator_review_required"}
            },
        }
        for position, item in enumerate(queued, start=1)
    ]
    excluded: list[dict[str, Any]] = []
    for item in sorted(normalized, key=lambda row: row["candidate_id"]):
        reasons: list[str] = []
        if not item["eligible"]:
            reasons.append("NOT_ELIGIBLE")
        if not item["operator_review_required"]:
            reasons.append("REVIEW_NOT_REQUIRED")
        if item["expiration_headroom_ms"] <= 0:
            reasons.append("EXPIRED")
        if reasons:
            excluded.append({"candidate_id": item["candidate_id"], "reason_codes": reasons})

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4ER",
        "input_hash": payload["artifact_hash"],
        "as_of_utc": payload["as_of_utc"],
        "priority_order": [
            "EXPIRATION_ASC",
            "EXPECTED_VALUE_DESC",
            "FRESHNESS_AGE_ASC",
            "REVIEW_COST_ASC",
            "CANDIDATE_ID_ASC",
        ],
        "queue": queue,
        "queued_count": len(queue),
        "excluded": excluded,
        "excluded_count": len(excluded),
        "operator_authorization_recorded": False,
        "paper_order_creation_authorized": False,
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
