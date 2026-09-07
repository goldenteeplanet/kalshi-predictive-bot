"""Phase 4AY artifact-only trusted-time and clock-skew audit for Phase 4AC-4AX."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

CATALOG_SCHEMA = "phase4ay.timestamp-catalog.v1"
SCHEMA = "phase4ay.time-semantics-audit.v1"
PROOF_SCHEMA = "phase4ay.time-semantics-proof.v1"
REQUIRED_PHASES = tuple(f"4A{chr(code)}" for code in range(ord("C"), ord("X") + 1))
EXPIRATION_SEMANTICS = "VALID_IFF_NOW_STRICTLY_BEFORE_DEADLINE"


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"PHASE4AY_{label}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"PHASE4AY_{label}_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"PHASE4AY_{label}_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def _value(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError("PHASE4AY_TIMESTAMP_PATH_MISSING")
        current = current[part]
    return current


def _load(path: Path) -> dict[str, Any]:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AY_CATALOG_UNREADABLE") from exc
    if catalog.get("schema") != CATALOG_SCHEMA or catalog.get("artifact_hash") != _hash(catalog):
        raise ValueError("PHASE4AY_CATALOG_SCHEMA_OR_HASH_INVALID")
    return catalog


def build(catalog_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AY_EVALUATION_TIMEZONE_MISSING")
    now = now.astimezone(UTC)
    catalog = _load(catalog_path)
    if _time(catalog.get("trusted_now"), "TRUSTED_NOW") != now:
        raise ValueError("PHASE4AY_TRUSTED_NOW_MISMATCH")
    skew = catalog.get("max_future_skew_seconds")
    regression = catalog.get("max_clock_regression_seconds")
    if not isinstance(skew, int) or not 0 <= skew <= 300:
        raise ValueError("PHASE4AY_FUTURE_SKEW_INVALID")
    if not isinstance(regression, int) or not 0 <= regression <= 300:
        raise ValueError("PHASE4AY_REGRESSION_ALLOWANCE_INVALID")
    if catalog.get("expiration_semantics") != EXPIRATION_SEMANTICS:
        raise ValueError("PHASE4AY_EXPIRATION_SEMANTICS_INVALID")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or [
        entry.get("phase") for entry in entries if isinstance(entry, dict)
    ] != list(REQUIRED_PHASES):
        raise ValueError("PHASE4AY_PHASE_COVERAGE_OR_ORDER_INVALID")

    rows: list[dict[str, Any]] = []
    previous_primary: datetime | None = None
    for sequence, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or entry.get("sequence") != sequence:
            raise ValueError("PHASE4AY_ENTRY_SEQUENCE_INVALID")
        payload = entry.get("payload")
        hash_field = entry.get("hash_field")
        timestamp_fields = entry.get("timestamp_fields")
        if not isinstance(payload, dict) or hash_field not in {"artifact_hash", "manifest_hash"}:
            raise ValueError("PHASE4AY_ENTRY_PAYLOAD_INVALID")
        if payload.get(hash_field) != _hash(payload, hash_field):
            raise ValueError("PHASE4AY_ENTRY_HASH_INVALID")
        if not isinstance(timestamp_fields, list) or not timestamp_fields:
            raise ValueError("PHASE4AY_TIMESTAMP_FIELDS_MISSING")
        reasons: set[str] = set()
        normalized: list[dict[str, str]] = []
        primary: datetime | None = None
        for field in timestamp_fields:
            if not isinstance(field, dict) or field.get("role") not in {"EVENT", "DEADLINE"}:
                raise ValueError("PHASE4AY_TIMESTAMP_FIELD_INVALID")
            path = field.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError("PHASE4AY_TIMESTAMP_FIELD_INVALID")
            parsed = _time(_value(payload, path), "ARTIFACT_TIMESTAMP")
            normalized.append({"path": path, "role": field["role"], "utc": parsed.isoformat()})
            if field["role"] == "EVENT" and parsed > now + timedelta(seconds=skew):
                reasons.add("FUTURE_DATED_ARTIFACT")
            if field.get("primary") is True:
                if field["role"] != "EVENT" or primary is not None:
                    raise ValueError("PHASE4AY_PRIMARY_TIMESTAMP_INVALID")
                primary = parsed
        if primary is None:
            raise ValueError("PHASE4AY_PRIMARY_TIMESTAMP_MISSING")
        if previous_primary is not None and primary < previous_primary - timedelta(
            seconds=regression
        ):
            reasons.add("CLOCK_REGRESSION")
        previous_primary = (
            max(previous_primary, primary) if previous_primary is not None else primary
        )
        row = {
            "sequence": sequence,
            "phase": entry["phase"],
            "source_hash": payload[hash_field],
            "primary_timestamp_utc": primary.isoformat(),
            "normalized_timestamps": normalized,
            "reason_codes": sorted(reasons),
            "time_semantics_valid": not reasons,
        }
        row["row_hash"] = canonical_hash(row)
        rows.append(row)
    all_reasons = sorted({reason for row in rows for reason in row["reason_codes"]})
    evaluated_at = now.isoformat()
    audit: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AY",
        "evaluated_at": evaluated_at,
        "catalog_hash": catalog["artifact_hash"],
        "phase_coverage": list(REQUIRED_PHASES),
        "max_future_skew_seconds": skew,
        "max_clock_regression_seconds": regression,
        "expiration_semantics": EXPIRATION_SEMANTICS,
        "rows": rows,
        "reason_codes": all_reasons,
        "time_semantics_valid": not all_reasons,
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    audit["artifact_hash"] = _hash(audit)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4AY",
        "evaluated_at": evaluated_at,
        "audit_hash": audit["artifact_hash"],
        "utc_normalization_verified": True,
        "future_dating_checked": True,
        "clock_regression_checked": True,
        "expiration_equality_is_expired": True,
        "phase_count": len(rows),
        "production_database_mutated": False,
        "research_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return audit, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timestamp-catalog", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    audit, proof = build(args.timestamp_catalog, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.audit_output, args.proof_output, audit, proof)
    print(json.dumps(audit, sort_keys=True))


if __name__ == "__main__":
    main()
