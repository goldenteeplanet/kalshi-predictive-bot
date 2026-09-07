"""Review supplied forecast-cache entries for poisoning indicators without using them."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dg.poisoning-input.v1"
REPORT_SCHEMA = "phase4dg.poisoning-report.v1"
ENTRY_SCHEMA = "phase4dg.cache-entry.v1"
MAX_ENTRIES = 10_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DG_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DG_HASH_INVALID") from exc
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DG_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DG_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DG_TIMESTAMP_INVALID")
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "expected", "evaluated_at", "entries", "artifact_hash"}
    if set(payload) != required:
        raise ValueError("PHASE4DG_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DG_INPUT_SCHEMA_OR_HASH_INVALID")
    evaluated_at = _time(payload["evaluated_at"])
    expected = payload["expected"]
    expected_fields = {"market_id", "ticker", "model_id", "model_hash", "minimum_evidence_at"}
    if not isinstance(expected, dict) or set(expected) != expected_fields:
        raise ValueError("PHASE4DG_EXPECTED_FIELDS_INVALID")
    for field in ("market_id", "ticker", "model_id"):
        if not isinstance(expected[field], str) or not expected[field]:
            raise ValueError("PHASE4DG_EXPECTED_IDENTITY_INVALID")
    _digest(expected["model_hash"])
    minimum_evidence = _time(expected["minimum_evidence_at"])
    if minimum_evidence > evaluated_at:
        raise ValueError("PHASE4DG_EXPECTED_FRESHNESS_INVALID")
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise ValueError("PHASE4DG_ENTRY_COUNT_INVALID")
    entry_fields = {
        "schema",
        "entry_id",
        "cache_key",
        "market_id",
        "ticker",
        "model_id",
        "model_hash",
        "feature_set_hash",
        "evidence_hash",
        "evidence_observed_at",
        "expires_at",
        "result_hash",
        "artifact_hash",
    }
    identifiers: set[str] = set()
    keys: dict[str, str] = {}
    findings = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != entry_fields:
            raise ValueError("PHASE4DG_ENTRY_FIELDS_INVALID")
        identifier = entry["entry_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DG_ENTRY_ID_INVALID")
        identifiers.add(identifier)
        reasons = []
        if entry["schema"] != ENTRY_SCHEMA:
            reasons.append("SCHEMA_DRIFT")
        if entry["artifact_hash"] != _hash(entry):
            reasons.append("ARTIFACT_HASH_MISMATCH")
        for field in (
            "cache_key",
            "model_hash",
            "feature_set_hash",
            "evidence_hash",
            "result_hash",
        ):
            _digest(entry[field])
        for field in ("market_id", "ticker", "model_id"):
            if not isinstance(entry[field], str) or not entry[field]:
                raise ValueError("PHASE4DG_ENTRY_IDENTITY_INVALID")
            if entry[field] != expected[field]:
                reasons.append(f"{field.upper()}_MISMATCH")
        if entry["model_hash"] != expected["model_hash"]:
            reasons.append("MODEL_HASH_MISMATCH")
        observed = _time(entry["evidence_observed_at"])
        expires = _time(entry["expires_at"])
        if observed < minimum_evidence:
            reasons.append("STALE_EVIDENCE")
        if expires < evaluated_at:
            reasons.append("EXPIRED_ENTRY")
        identity = {
            "schema": ENTRY_SCHEMA,
            "market_id": entry["market_id"],
            "ticker": entry["ticker"],
            "model_id": entry["model_id"],
            "model_hash": entry["model_hash"],
            "feature_set_hash": entry["feature_set_hash"],
            "evidence_hash": entry["evidence_hash"],
            "result_hash": entry["result_hash"],
        }
        computed_key = canonical_hash(identity)
        if entry["cache_key"] != computed_key:
            reasons.append("CONTENT_ADDRESS_MISMATCH")
        content_hash = canonical_hash(identity)
        prior = keys.get(entry["cache_key"])
        if prior is not None and prior != content_hash:
            reasons.append("CACHE_KEY_COLLISION_OR_ALIAS")
        keys[entry["cache_key"]] = content_hash
        findings.append(
            {
                "entry_id": identifier,
                "safe": not reasons,
                "computed_cache_key": computed_key,
                "reasons": reasons,
            }
        )
    unsafe = [finding["entry_id"] for finding in findings if not finding["safe"]]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DG",
        "input_hash": payload["artifact_hash"],
        "status": "UNSAFE" if unsafe else "SAFE",
        "entry_findings": findings,
        "unsafe_entry_ids": unsafe,
        "cache_entries_consumed": 0,
        "cache_entries_written": 0,
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
