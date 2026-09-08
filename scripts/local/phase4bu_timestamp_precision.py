"""Harmonize wall-clock and monotonic timestamp evidence deterministically."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bu.timestamp-evidence-input.v1"
SCHEMA = "phase4bu.timestamp-harmonization.v1"
PROOF_SCHEMA = "phase4bu.timestamp-precision-proof.v1"
STAGES = (
    "COLLECTION",
    "SNAPSHOT",
    "FORECAST",
    "RANKING",
    "POSITION_SIZING",
    "ADVANCED_RISK",
    "APPROVAL",
    "PAPER_ROUTING",
    "OBSERVABILITY",
)
REQUIRED_FRACTION_DIGITS = 6
MAX_DURATION_NS = 86_400_000_000_000
FRACTION = re.compile(r"T\d{2}:\d{2}:\d{2}(?:\.(\d+))?(?:Z|[+-]\d{2}:\d{2})$")


def _hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _wall(value: Any) -> tuple[datetime, int]:
    if not isinstance(value, str):
        raise ValueError("PHASE4BU_TIMESTAMP_INVALID")
    match = FRACTION.search(value)
    if match is None:
        raise ValueError("PHASE4BU_TIMESTAMP_FORMAT_OR_TIMEZONE_INVALID")
    digits = len(match.group(1) or "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4BU_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("PHASE4BU_TIMESTAMP_TIMEZONE_MISSING")
    return parsed.astimezone(UTC), digits


def _canonical(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BU_INPUT_SCHEMA_OR_HASH_INVALID")
    rows = payload.get("samples")
    stages = (
        [row.get("stage") for row in rows if isinstance(row, dict)]
        if isinstance(rows, list)
        else []
    )
    if not isinstance(rows, list) or stages != list(STAGES):
        raise ValueError("PHASE4BU_STAGE_COVERAGE_OR_ORDER_INVALID")
    expected = {
        "stage",
        "wall_started_at",
        "wall_ended_at",
        "monotonic_started_ns",
        "monotonic_ended_ns",
        "declared_fraction_digits",
        "evidence_hash",
    }
    normalized: list[dict[str, Any]] = []
    previous_wall: datetime | None = None
    previous_monotonic: int | None = None
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError("PHASE4BU_SAMPLE_FIELDS_INVALID")
        started, start_digits = _wall(row["wall_started_at"])
        ended, end_digits = _wall(row["wall_ended_at"])
        declared = row["declared_fraction_digits"]
        if (
            declared != REQUIRED_FRACTION_DIGITS
            or start_digits != declared
            or end_digits != declared
        ):
            raise ValueError("PHASE4BU_PRECISION_LOSS_OR_MISMATCH")
        mono_start, mono_end = row["monotonic_started_ns"], row["monotonic_ended_ns"]
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (mono_start, mono_end)
        ):
            raise ValueError("PHASE4BU_MONOTONIC_TYPE_INVALID")
        if ended < started or mono_end < mono_start:
            raise ValueError("PHASE4BU_CLOCK_REGRESSION")
        if previous_wall is not None and started < previous_wall:
            raise ValueError("PHASE4BU_WALL_SEQUENCE_REGRESSION")
        if previous_monotonic is not None and mono_start < previous_monotonic:
            raise ValueError("PHASE4BU_MONOTONIC_SEQUENCE_REGRESSION")
        monotonic_duration = mono_end - mono_start
        if monotonic_duration > MAX_DURATION_NS:
            raise ValueError("PHASE4BU_DURATION_BOUND_EXCEEDED")
        wall_duration = int((ended - started).total_seconds() * 1_000_000_000)
        if wall_duration != monotonic_duration:
            raise ValueError("PHASE4BU_WALL_MONOTONIC_DURATION_MISMATCH")
        if not isinstance(row["evidence_hash"], str) or len(row["evidence_hash"]) != 64:
            raise ValueError("PHASE4BU_EVIDENCE_HASH_INVALID")
        normalized.append(
            {
                "stage": row["stage"],
                "wall_started_at_utc": _canonical(started),
                "wall_ended_at_utc": _canonical(ended),
                "monotonic_started_ns": mono_start,
                "monotonic_ended_ns": mono_end,
                "duration_ns": monotonic_duration,
                "fraction_digits": REQUIRED_FRACTION_DIGITS,
                "evidence_hash": row["evidence_hash"],
            }
        )
        previous_wall, previous_monotonic = ended, mono_end
    harmonized: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BU",
        "input_hash": payload["artifact_hash"],
        "samples": normalized,
        "canonical_timezone": "UTC",
        "canonical_fraction_digits": REQUIRED_FRACTION_DIGITS,
        "production_records_created": 0,
        "execution_authorized": False,
    }
    harmonized["artifact_hash"] = _hash(harmonized)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4BU",
        "harmonization_hash": harmonized["artifact_hash"],
        "sample_count": len(normalized),
        "wall_monotonic_agreement": True,
        "clock_regression_detected": False,
        "precision_loss_detected": False,
        "canonicalization_only": True,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return harmonized, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timestamp-input", type=Path, required=True)
    parser.add_argument("--harmonized-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.timestamp_input.read_text(encoding="utf-8"))
    harmonized, proof = build(payload)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.harmonized_output, args.proof_output, harmonized, proof)
    print(json.dumps(harmonized, sort_keys=True))


if __name__ == "__main__":
    main()
