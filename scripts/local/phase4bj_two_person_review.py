"""Phase 4BJ validation-only optional two-person review protocol."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

TARGET_SCHEMA = "phase4bj.review-target.v1"
APPROVAL_SCHEMA = "phase4bj.external-two-person-approval.v1"
SCHEMA = "phase4bj.two-person-review-validation.v1"
REFUSAL_SCHEMA = "phase4bj.two-person-review-refusal.v1"


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load(path: Path, schema: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"PHASE4BJ_{label}_UNREADABLE") from exc
    if payload.get("schema") != schema or payload.get("artifact_hash") != _hash(payload):
        raise ValueError(f"PHASE4BJ_{label}_SCHEMA_OR_HASH_INVALID")
    return payload


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"PHASE4BJ_{label}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"PHASE4BJ_{label}_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"PHASE4BJ_{label}_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def build(
    target_path: Path, approval_path: Path, *, now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BJ_EVALUATION_TIMEZONE_MISSING")
    now = now.astimezone(UTC)
    target = _load(target_path, TARGET_SCHEMA, "TARGET")
    approval = _load(approval_path, APPROVAL_SCHEMA, "APPROVAL")
    expected_rows = target.get("row_hashes")
    approved_rows = approval.get("rows")
    if not isinstance(expected_rows, list) or len(expected_rows) != len(set(expected_rows)):
        raise ValueError("PHASE4BJ_TARGET_ROWS_INVALID")
    if not isinstance(approved_rows, list):
        raise ValueError("PHASE4BJ_APPROVAL_ROWS_INVALID")
    reasons: list[str] = []
    if (
        approval.get("externally_supplied") is not True
        or approval.get("generated_by_guarded_tooling") is not False
    ):
        reasons.append("APPROVAL_PROVENANCE_INVALID")
    if approval.get("target_hash") != target["artifact_hash"]:
        reasons.append("TARGET_BINDING_MISMATCH")
    target_expiration = _time(target.get("expires_at"), "TARGET_EXPIRATION")
    approval_expiration = _time(approval.get("expires_at"), "APPROVAL_EXPIRATION")
    shared_expiration = min(target_expiration, approval_expiration)
    if now >= shared_expiration:
        reasons.append("SHARED_EXPIRATION_REACHED")
    if approval.get("revoked") is not False:
        reasons.append("APPROVAL_REVOKED")
    row_hashes: list[str] = []
    reviewer_pairs: list[dict[str, str]] = []
    for row in approved_rows:
        if not isinstance(row, dict):
            raise ValueError("PHASE4BJ_APPROVAL_ROW_INVALID")
        row_hash = row.get("row_hash")
        first, second = row.get("reviewer_one"), row.get("reviewer_two")
        if (
            not isinstance(row_hash, str)
            or not isinstance(first, dict)
            or not isinstance(second, dict)
        ):
            raise ValueError("PHASE4BJ_APPROVAL_ROW_INVALID")
        row_hashes.append(row_hash)
        identities = (first.get("identity"), second.get("identity"))
        if any(not isinstance(identity, str) or not identity for identity in identities):
            reasons.append("REVIEWER_IDENTITY_INVALID")
        elif identities[0] == identities[1]:
            reasons.append("DUPLICATE_OR_SELF_REVIEW")
        timestamps = (
            _time(first.get("reviewed_at"), "REVIEWER_ONE_TIMESTAMP"),
            _time(second.get("reviewed_at"), "REVIEWER_TWO_TIMESTAMP"),
        )
        if timestamps[0] == timestamps[1]:
            reasons.append("REVIEW_TIMESTAMPS_NOT_SEPARATE")
        if any(timestamp > now or timestamp >= shared_expiration for timestamp in timestamps):
            reasons.append("REVIEW_TIMESTAMP_OUTSIDE_WINDOW")
        if first.get("decision") != "APPROVE" or second.get("decision") != "APPROVE":
            reasons.append("INDEPENDENT_ROW_APPROVAL_MISSING")
        reviewer_pairs.append(
            {
                "reviewer_one_hash": canonical_hash(identities[0]),
                "reviewer_two_hash": canonical_hash(identities[1]),
            }
        )
    if sorted(row_hashes) != sorted(expected_rows) or len(row_hashes) != len(set(row_hashes)):
        reasons.append("ROW_BINDING_OR_DUPLICATION_INVALID")
    reasons = sorted(set(reasons))
    evaluated_at = now.isoformat()
    validation: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BJ",
        "evaluated_at": evaluated_at,
        "target_hash": target["artifact_hash"],
        "approval_hash": approval["artifact_hash"],
        "shared_expiration": shared_expiration.isoformat(),
        "reviewer_pairs": reviewer_pairs,
        "approved_row_hashes": sorted(row_hashes),
        "reason_codes": reasons,
        "two_person_review_valid": not reasons,
        "approval_generated": False,
        "production_execution_authorized": False,
        "execution_authorized": False,
    }
    validation["artifact_hash"] = _hash(validation)
    refusal: dict[str, Any] = {
        "schema": REFUSAL_SCHEMA,
        "phase": "4BJ",
        "evaluated_at": evaluated_at,
        "validation_hash": validation["artifact_hash"],
        "reason_codes": reasons,
        "refusal_required": bool(reasons),
        "approval_generated": False,
        "production_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    refusal["artifact_hash"] = _hash(refusal)
    return validation, refusal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-target", type=Path, required=True)
    parser.add_argument("--external-approval", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--validation-output", type=Path, required=True)
    parser.add_argument("--refusal-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    validation, refusal = build(args.review_target, args.external_approval, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.validation_output, args.refusal_output, validation, refusal)
    print(json.dumps(validation, sort_keys=True))


if __name__ == "__main__":
    main()
