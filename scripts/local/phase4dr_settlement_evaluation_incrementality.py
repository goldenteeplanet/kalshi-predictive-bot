"""Recompute only affected settlement metrics with full-rebuild equivalence offline."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dr.incremental-evaluation-input.v1"
REPORT_SCHEMA = "phase4dr.incremental-evaluation-report.v1"
MAX_SETTLEMENTS = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _probability(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4DR_PROBABILITY_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DR_PROBABILITY_INVALID") from exc
    if not parsed.is_finite() or not Decimal(0) <= parsed <= Decimal(1):
        raise ValueError("PHASE4DR_PROBABILITY_INVALID")
    return parsed


def _render(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != {
        "forecast_id",
        "market_id",
        "probability",
        "outcome",
    }:
        raise ValueError("PHASE4DR_SETTLEMENT_FIELDS_INVALID")
    if any(
        not isinstance(record[field], str) or not record[field]
        for field in ("forecast_id", "market_id")
    ):
        raise ValueError("PHASE4DR_SETTLEMENT_IDENTITY_INVALID")
    _probability(record["probability"])
    if record["outcome"] not in {0, 1} or isinstance(record["outcome"], bool):
        raise ValueError("PHASE4DR_OUTCOME_INVALID")
    return record


def _metrics(records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records.values():
        grouped.setdefault(record["market_id"], []).append(record)
    result = {}
    for market in sorted(grouped):
        rows = grouped[market]
        brier = sum(
            ((_probability(row["probability"]) - Decimal(row["outcome"])) ** 2 for row in rows),
            Decimal(0),
        )
        correct = sum(
            (_probability(row["probability"]) >= Decimal("0.5")) == bool(row["outcome"])
            for row in rows
        )
        result[market] = {
            "settlement_count": len(rows),
            "brier_sum": _render(brier),
            "correct_count": correct,
        }
    return result


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "settlements", "previous_metrics", "updates", "artifact_hash"}
    if set(payload) != required:
        raise ValueError("PHASE4DR_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DR_INPUT_SCHEMA_OR_HASH_INVALID")
    settlements = payload["settlements"]
    if not isinstance(settlements, list) or not settlements or len(settlements) > MAX_SETTLEMENTS:
        raise ValueError("PHASE4DR_SETTLEMENT_COUNT_INVALID")
    records: dict[str, dict[str, Any]] = {}
    for record in settlements:
        _validate_record(record)
        if record["forecast_id"] in records:
            raise ValueError("PHASE4DR_FORECAST_ID_DUPLICATE")
        records[record["forecast_id"]] = record
    baseline_before = _metrics(records)
    if payload["previous_metrics"] != baseline_before:
        raise ValueError("PHASE4DR_PREVIOUS_METRICS_MISMATCH")
    updates = payload["updates"]
    if not isinstance(updates, list) or not updates or len(updates) > MAX_SETTLEMENTS:
        raise ValueError("PHASE4DR_UPDATE_COUNT_INVALID")
    affected: set[str] = set()
    updated_ids: set[str] = set()
    working = dict(records)
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"operation", "record", "forecast_id"}:
            raise ValueError("PHASE4DR_UPDATE_FIELDS_INVALID")
        identifier = update["forecast_id"]
        if not isinstance(identifier, str) or not identifier or identifier in updated_ids:
            raise ValueError("PHASE4DR_UPDATE_ID_INVALID")
        updated_ids.add(identifier)
        if update["operation"] == "UPSERT":
            record = _validate_record(update["record"])
            if record["forecast_id"] != identifier:
                raise ValueError("PHASE4DR_UPDATE_IDENTITY_MISMATCH")
            if identifier in working:
                affected.add(working[identifier]["market_id"])
            affected.add(record["market_id"])
            working[identifier] = record
        elif update["operation"] == "DELETE":
            if update["record"] is not None or identifier not in working:
                raise ValueError("PHASE4DR_DELETE_INVALID")
            affected.add(working[identifier]["market_id"])
            del working[identifier]
        else:
            raise ValueError("PHASE4DR_OPERATION_INVALID")
    if not working:
        raise ValueError("PHASE4DR_EMPTY_EVALUATION_INVALID")
    full_after = _metrics(working)
    incremental = dict(baseline_before)
    for market in affected:
        if market in full_after:
            incremental[market] = full_after[market]
        else:
            incremental.pop(market, None)
    if incremental != full_after:
        raise ValueError("PHASE4DR_FULL_EQUIVALENCE_FAILED")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DR",
        "input_hash": payload["artifact_hash"],
        "affected_market_ids": sorted(affected),
        "reused_market_ids": sorted(set(full_after) - affected),
        "metrics": incremental,
        "incremental_metrics_hash": canonical_hash(incremental),
        "full_metrics_hash": canonical_hash(full_after),
        "full_equivalence": True,
        "evaluation_records_created": 0,
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
