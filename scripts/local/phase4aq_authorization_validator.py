"""Phase 4AQ validator for externally supplied one-attempt simulation authorization."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

AUTH_SCHEMA = "phase4aq.one-attempt-operator-authorization.v1"
SCHEMA = "phase4aq.authorization-validation.v1"
MANIFEST_SCHEMA = "phase4aq.authorization-refusal-manifest.v1"
AK_SCHEMA = "phase4ak.settlement-mutation-readiness-envelope.v1"
AL_SCHEMA = "phase4al.offline-protocol-simulation-report.v1"


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load(path: Path, schema: str, hash_field: str = "artifact_hash") -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AQ_ARTIFACT_UNREADABLE") from exc
    if payload.get("schema") != schema:
        raise ValueError("PHASE4AQ_ARTIFACT_SCHEMA_INVALID")
    if payload.get(hash_field) != _hash(payload, hash_field):
        raise ValueError("PHASE4AQ_ARTIFACT_HASH_MISMATCH")
    return payload


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"PHASE4AQ_{label}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"PHASE4AQ_{label}_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"PHASE4AQ_{label}_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def build(
    authorization_path: Path,
    readiness_path: Path,
    simulation_path: Path,
    *,
    executor_build_identity_hash: str,
    previous_attempt_ids: list[str],
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AQ_EVALUATION_TIMEZONE_MISSING")
    if not executor_build_identity_hash or len(executor_build_identity_hash) != 64:
        raise ValueError("PHASE4AQ_EXECUTOR_BUILD_IDENTITY_INVALID")
    if len(previous_attempt_ids) != len(set(previous_attempt_ids)):
        raise ValueError("PHASE4AQ_PREVIOUS_ATTEMPT_HISTORY_DUPLICATED")
    authorization = _load(authorization_path, AUTH_SCHEMA)
    readiness = _load(readiness_path, AK_SCHEMA)
    simulation = _load(simulation_path, AL_SCHEMA)
    now = now.astimezone(UTC)
    reasons: list[str] = []
    attempt_id = authorization.get("attempt_id")
    operator = authorization.get("operator_identity")
    reviewer = authorization.get("second_reviewer_identity")
    if (
        authorization.get("externally_supplied") is not True
        or authorization.get("generated_by_guarded_tooling") is not False
    ):
        reasons.append("AUTHORIZATION_NOT_EXTERNALLY_SUPPLIED")
    if not isinstance(attempt_id, str) or not attempt_id:
        reasons.append("ATTEMPT_ID_INVALID")
    elif attempt_id in previous_attempt_ids:
        reasons.append("ATTEMPT_ID_ALREADY_USED")
    if not isinstance(operator, str) or not operator.strip():
        reasons.append("OPERATOR_IDENTITY_INVALID")
    if reviewer is not None and (
        not isinstance(reviewer, str) or not reviewer.strip() or reviewer == operator
    ):
        reasons.append("SECOND_REVIEWER_IDENTITY_INVALID")
    if authorization.get("revoked") is not False or authorization.get("revoked_at") is not None:
        reasons.append("AUTHORIZATION_REVOKED")
    expected_rows = sorted(row.get("readiness_row_hash") for row in readiness.get("rows", []))
    supplied_rows = authorization.get("readiness_row_hashes")
    if supplied_rows != expected_rows or len(expected_rows) != len(set(expected_rows)):
        reasons.append("READINESS_ROW_BINDING_MISMATCH")
    bindings = {
        "readiness_envelope_hash": readiness.get("artifact_hash"),
        "simulation_report_hash": simulation.get("artifact_hash"),
        "production_database_identity_hash": canonical_hash(
            readiness.get("production_database_identity")
        ),
        "executor_build_identity_hash": executor_build_identity_hash,
    }
    for key, expected in bindings.items():
        if authorization.get(key) != expected:
            reasons.append(f"{key.upper()}_MISMATCH")
    if simulation.get("input_hashes", {}).get("phase4ak_readiness") != readiness.get(
        "artifact_hash"
    ):
        reasons.append("SIMULATION_READINESS_LINEAGE_MISMATCH")
    if simulation.get("simulation_outcome") != "SIMULATION_COMMITTED":
        reasons.append("SIMULATION_NOT_SUCCESSFUL")
    readiness_deadline = _time(readiness.get("approval_expires_at"), "READINESS_EXPIRATION")
    authorization_deadline = _time(authorization.get("expires_at"), "AUTHORIZATION_EXPIRATION")
    if authorization_deadline > readiness_deadline:
        reasons.append("AUTHORIZATION_EXCEEDS_EARLIEST_EXPIRATION")
    if now >= authorization_deadline:
        reasons.append("AUTHORIZATION_EXPIRED")
    required_false = (
        "exchange_authorized",
        "orders_authorized",
        "paper_orders_authorized",
        "production_execution_authorized",
    )
    if authorization.get("scope") != "ONE_DISPOSABLE_SIMULATION_ATTEMPT":
        reasons.append("AUTHORIZATION_SCOPE_INVALID")
    if any(authorization.get(field) is not False for field in required_false):
        reasons.append("PROHIBITED_AUTHORIZATION_SCOPE_PRESENT")
    reasons = sorted(set(reasons))
    valid = not reasons
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AQ",
        "evaluated_at": now.isoformat(),
        "authorization_artifact_hash": authorization["artifact_hash"],
        "attempt_id": attempt_id,
        "operator_identity_hash": canonical_hash(operator) if isinstance(operator, str) else None,
        "second_reviewer_identity_hash": canonical_hash(reviewer)
        if isinstance(reviewer, str)
        else None,
        "validated_bindings": dict(sorted(bindings.items())),
        "effective_expiration": min(readiness_deadline, authorization_deadline).isoformat(),
        "reason_codes": reasons,
        "authorization_valid_for_disposable_test_attempt": valid,
        "authorization_valid_for_production": False,
        "production_database_mutated": False,
        "research_database_mutated": False,
        "database_mutation_performed": False,
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    pair_id = canonical_hash(
        {"authorization": authorization["artifact_hash"], "now": now, "reasons": reasons}
    )
    report["publication_pair_id"] = pair_id
    report["artifact_hash"] = _hash(report)
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "phase": "4AQ",
        "publication_pair_id": pair_id,
        "validation_artifact_hash": report["artifact_hash"],
        "refusal_count": len(reasons),
        "refusal_reasons": reasons,
        "one_attempt_only": True,
        "authorization_generated_by_validator": False,
        "production_execution_authorized": False,
        "exchange_or_order_authorized": False,
    }
    manifest["manifest_hash"] = _hash(manifest, "manifest_hash")
    return report, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--phase4ak-readiness", type=Path, required=True)
    parser.add_argument("--phase4al-simulation", type=Path, required=True)
    parser.add_argument("--executor-build-identity-hash", required=True)
    parser.add_argument("--previous-attempt-id", action="append", default=[])
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--validation-output", type=Path, required=True)
    parser.add_argument("--refusal-output", type=Path, required=True)
    args = parser.parse_args()
    now = _time(args.evaluation_time, "EVALUATION_TIME")
    report, manifest = build(
        args.authorization,
        args.phase4ak_readiness,
        args.phase4al_simulation,
        executor_build_identity_hash=args.executor_build_identity_hash,
        previous_attempt_ids=args.previous_attempt_id,
        now=now,
    )
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.validation_output, args.refusal_output, report, manifest)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
