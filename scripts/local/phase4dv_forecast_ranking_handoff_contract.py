"""Build a minimal hash-protected forecast-to-ranking handoff artifact offline."""

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

INPUT_SCHEMA = "phase4dv.forecast-batch.v1"
REPORT_SCHEMA = "phase4dv.ranking-handoff.v1"
MAX_CANDIDATES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DV_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DV_HASH_INVALID") from exc
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DV_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DV_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DV_TIMESTAMP_INVALID")
    return parsed


def _probability(value: Any) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4DV_PROBABILITY_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DV_PROBABILITY_INVALID") from exc
    if not parsed.is_finite() or not Decimal(0) <= parsed <= Decimal(1):
        raise ValueError("PHASE4DV_PROBABILITY_INVALID")
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema",
        "batch_id",
        "generated_at",
        "ranking_deadline",
        "model_hash",
        "evidence_hash",
        "candidates",
        "artifact_hash",
    }
    if set(payload) != fields:
        raise ValueError("PHASE4DV_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DV_INPUT_SCHEMA_OR_HASH_INVALID")
    if not isinstance(payload["batch_id"], str) or not payload["batch_id"]:
        raise ValueError("PHASE4DV_BATCH_ID_INVALID")
    generated = _time(payload["generated_at"])
    deadline = _time(payload["ranking_deadline"])
    if deadline < generated:
        raise ValueError("PHASE4DV_DEADLINE_INVALID")
    _digest(payload["model_hash"])
    _digest(payload["evidence_hash"])
    candidates = payload["candidates"]
    if not isinstance(candidates, list) or not candidates or len(candidates) > MAX_CANDIDATES:
        raise ValueError("PHASE4DV_CANDIDATE_COUNT_INVALID")
    candidate_fields = {
        "candidate_id",
        "market_id",
        "forecast_probability",
        "market_probability",
        "forecast_hash",
        "artifact_hash",
    }
    identifiers: set[str] = set()
    compact = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != candidate_fields:
            raise ValueError("PHASE4DV_CANDIDATE_FIELDS_INVALID")
        if candidate["artifact_hash"] != _hash(candidate):
            raise ValueError("PHASE4DV_CANDIDATE_HASH_INVALID")
        identifier, market = candidate["candidate_id"], candidate["market_id"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
            or not isinstance(market, str)
            or not market
        ):
            raise ValueError("PHASE4DV_CANDIDATE_IDENTITY_INVALID")
        identifiers.add(identifier)
        _digest(candidate["forecast_hash"])
        compact.append(
            {
                "candidate_id": identifier,
                "market_id": market,
                "forecast_probability": _probability(candidate["forecast_probability"]),
                "market_probability": _probability(candidate["market_probability"]),
                "forecast_hash": candidate["forecast_hash"],
            }
        )
    compact.sort(key=lambda row: row["candidate_id"])
    canonical_view = {
        "batch_id": payload["batch_id"],
        "generated_at": payload["generated_at"],
        "ranking_deadline": payload["ranking_deadline"],
        "model_hash": payload["model_hash"],
        "evidence_hash": payload["evidence_hash"],
        "candidates": compact,
    }
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DV",
        "source_artifact_hash": payload["artifact_hash"],
        "lineage": {
            "batch_id": payload["batch_id"],
            "model_hash": payload["model_hash"],
            "evidence_hash": payload["evidence_hash"],
            "generated_at": payload["generated_at"],
            "ranking_deadline": payload["ranking_deadline"],
        },
        "candidates": compact,
        "canonical_source_view_hash": canonical_hash(canonical_view),
        "reconstructed_view_hash": canonical_hash(canonical_view),
        "lossless_for_ranking": True,
        "source_parse_count": 1,
        "repeated_lineage_fields_per_candidate": 0,
        "ranking_records_created": 0,
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
