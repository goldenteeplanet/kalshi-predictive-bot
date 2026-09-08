"""Generate artifact-only idempotency keys and suppress duplicate paper intents."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4em.intent-input.v1"
REPORT_SCHEMA = "phase4em.intent-report.v1"
HASH_FIELDS = (
    "forecast_artifact_hash",
    "snapshot_artifact_hash",
    "ranking_artifact_hash",
    "position_sizing_artifact_hash",
    "advanced_risk_artifact_hash",
    "approval_artifact_hash",
)
IDENTITY_FIELDS = (
    "database_identity",
    *HASH_FIELDS,
    "market_ticker",
    "side",
    "quantity",
    "price_cents",
    "expires_at",
)


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _digest(value: Any, error: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(error)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(error) from exc


def _timestamp(value: Any) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4EM_EXPIRATION_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4EM_EXPIRATION_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("PHASE4EM_EXPIRATION_INVALID")


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "intents", "artifact_hash"}:
        raise ValueError("PHASE4EM_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EM_INPUT_SCHEMA_OR_HASH_INVALID")
    intents = payload.get("intents")
    if not isinstance(intents, list) or not intents:
        raise ValueError("PHASE4EM_INTENTS_EMPTY")
    seen_ids = set()
    keyed = []
    required = {"intent_id", *IDENTITY_FIELDS}
    for intent in intents:
        if not isinstance(intent, dict) or set(intent) != required:
            raise ValueError("PHASE4EM_INTENT_FIELDS_INVALID")
        identifier = intent["intent_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen_ids:
            raise ValueError("PHASE4EM_INTENT_ID_INVALID")
        seen_ids.add(identifier)
        if not isinstance(intent["database_identity"], str) or not intent["database_identity"]:
            raise ValueError("PHASE4EM_DATABASE_IDENTITY_INVALID")
        for field in HASH_FIELDS:
            _digest(intent[field], "PHASE4EM_LINEAGE_HASH_INVALID")
        if not isinstance(intent["market_ticker"], str) or not intent["market_ticker"]:
            raise ValueError("PHASE4EM_MARKET_INVALID")
        if intent["side"] not in {"YES", "NO"}:
            raise ValueError("PHASE4EM_SIDE_INVALID")
        for field in ("quantity", "price_cents"):
            value = intent[field]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("PHASE4EM_TERMS_INVALID")
        if intent["price_cents"] > 99:
            raise ValueError("PHASE4EM_TERMS_INVALID")
        _timestamp(intent["expires_at"])
        identity = {field: intent[field] for field in IDENTITY_FIELDS}
        keyed.append(
            {
                "intent_id": identifier,
                "idempotency_key": canonical_hash(identity),
                "identity": identity,
            }
        )
    primary_by_key = {}
    for row in keyed:
        primary_by_key[row["idempotency_key"]] = min(
            row["intent_id"], primary_by_key.get(row["idempotency_key"], row["intent_id"])
        )
    results = []
    for row in sorted(keyed, key=lambda item: item["intent_id"]):
        primary = primary_by_key[row["idempotency_key"]]
        duplicate = row["intent_id"] != primary
        results.append(
            {
                **row,
                "status": "SUPPRESS_DUPLICATE" if duplicate else "PRIMARY",
                "primary_intent_id": primary,
            }
        )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EM",
        "input_hash": payload["artifact_hash"],
        "identity_fields": list(IDENTITY_FIELDS),
        "results": results,
        "primary_count": sum(row["status"] == "PRIMARY" for row in results),
        "suppressed_duplicate_count": sum(row["status"] == "SUPPRESS_DUPLICATE" for row in results),
        "idempotency_records_written": 0,
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
