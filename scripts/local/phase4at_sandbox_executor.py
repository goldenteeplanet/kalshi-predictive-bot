"""Phase 4AT structurally disposable-only settlement sandbox prototype."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash, settlement_lineage_hash

SCHEMA = "phase4at.sandbox-simulation-receipt.v1"
MANIFEST_SCHEMA = "phase4at.sandbox-safety-proof.v1"
PROTECTED_SCHEMA = "phase4at.protected-database-identities.v1"
AK_SCHEMA = "phase4ak.settlement-mutation-readiness-envelope.v1"
AL_SCHEMA = "phase4al.offline-protocol-simulation-report.v1"
AQ_SCHEMA = "phase4aq.authorization-validation.v1"
MARKER_SCHEMA = "phase4al.disposable-simulation-database.v1"
OPERATION = "SET_CANONICAL_SETTLED_AT_IF_NULL"


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load(path: Path, schema: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AT_ARTIFACT_UNREADABLE") from exc
    if payload.get("schema") != schema or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4AT_ARTIFACT_SCHEMA_OR_HASH_INVALID")
    return payload


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"PHASE4AT_{label}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"PHASE4AT_{label}_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"PHASE4AT_{label}_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def _validate_identity(sandbox: Path, protected: dict[str, Any]) -> dict[str, Any]:
    resolved = sandbox.resolve(strict=True)
    stat = resolved.stat()
    identities = protected.get("identities")
    if not isinstance(identities, list) or not identities:
        raise ValueError("PHASE4AT_PROTECTED_IDENTITIES_MISSING")
    kinds = {item.get("kind") for item in identities if isinstance(item, dict)}
    if not {"PRODUCTION", "RESEARCH"} <= kinds:
        raise ValueError("PHASE4AT_PRODUCTION_OR_RESEARCH_IDENTITY_MISSING")
    protected_snapshots: list[dict[str, Any]] = []
    for item in identities:
        if not isinstance(item, dict):
            raise ValueError("PHASE4AT_PROTECTED_IDENTITY_INVALID")
        try:
            protected_path = Path(item["resolved_path"]).resolve(strict=True)
            protected_pair = (int(item["device"]), int(item["inode"]))
            expected_size = int(item["size"])
            expected_mtime_ns = int(item["mtime_ns"])
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ValueError("PHASE4AT_PROTECTED_IDENTITY_INVALID") from exc
        actual = protected_path.stat()
        if protected_pair != (actual.st_dev, actual.st_ino) or (
            expected_size,
            expected_mtime_ns,
        ) != (actual.st_size, actual.st_mtime_ns):
            raise ValueError("PHASE4AT_PROTECTED_IDENTITY_DRIFT")
        if resolved == protected_path or (stat.st_dev, stat.st_ino) == protected_pair:
            raise ValueError("PHASE4AT_SANDBOX_MATCHES_PROTECTED_IDENTITY")
        if resolved.parent == protected_path.parent:
            raise ValueError("PHASE4AT_SANDBOX_IN_PROTECTED_DIRECTORY")
        protected_snapshots.append(
            {
                "kind": item["kind"],
                "resolved_path_hash": canonical_hash(str(protected_path)),
                "device": actual.st_dev,
                "inode": actual.st_ino,
                "size": actual.st_size,
                "mtime_ns": actual.st_mtime_ns,
            }
        )
    return {
        "sandbox_resolved_path_hash": canonical_hash(str(resolved)),
        "sandbox_device": stat.st_dev,
        "sandbox_inode": stat.st_ino,
        "protected_identity_count": len(identities),
        "protected_snapshot_hash": canonical_hash(
            sorted(protected_snapshots, key=lambda value: value["kind"])
        ),
        "all_identities_distinct": True,
    }


def build(
    sandbox_db: Path,
    protected_identities_path: Path,
    readiness_path: Path,
    simulation_path: Path,
    authorization_validation_path: Path,
    *,
    executor_build_identity_hash: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AT_EVALUATION_TIMEZONE_MISSING")
    now = now.astimezone(UTC)
    if len(executor_build_identity_hash) != 64 or any(
        character not in "0123456789abcdef" for character in executor_build_identity_hash
    ):
        raise ValueError("PHASE4AT_EXECUTOR_BUILD_IDENTITY_INVALID")
    protected = _load(protected_identities_path, PROTECTED_SCHEMA)
    readiness = _load(readiness_path, AK_SCHEMA)
    simulation = _load(simulation_path, AL_SCHEMA)
    authorization = _load(authorization_validation_path, AQ_SCHEMA)
    isolation = _validate_identity(sandbox_db, protected)
    if readiness.get("readiness_state") != "READY_FOR_SEPARATE_EXECUTOR_DESIGN":
        raise ValueError("PHASE4AT_READINESS_NOT_READY")
    rows = readiness.get("rows")
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("readiness_result") != "READY":
        raise ValueError("PHASE4AT_EXACTLY_ONE_READY_ROW_REQUIRED")
    row = rows[0]
    if (
        simulation.get("simulation_outcome") != "SIMULATION_COMMITTED"
        or simulation.get("input_hashes", {}).get("phase4ak_readiness")
        != readiness["artifact_hash"]
    ):
        raise ValueError("PHASE4AT_SIMULATION_LINEAGE_INVALID")
    if authorization.get("authorization_valid_for_disposable_test_attempt") is not True:
        raise ValueError("PHASE4AT_TEST_AUTHORIZATION_INVALID")
    bindings = authorization.get("validated_bindings", {})
    if bindings.get("readiness_envelope_hash") != readiness["artifact_hash"]:
        raise ValueError("PHASE4AT_AUTHORIZATION_READINESS_MISMATCH")
    if bindings.get("simulation_report_hash") != simulation["artifact_hash"]:
        raise ValueError("PHASE4AT_AUTHORIZATION_SIMULATION_MISMATCH")
    if bindings.get("executor_build_identity_hash") != executor_build_identity_hash:
        raise ValueError("PHASE4AT_EXECUTOR_BUILD_IDENTITY_MISMATCH")
    if bindings.get("production_database_identity_hash") != canonical_hash(
        readiness.get("production_database_identity")
    ):
        raise ValueError("PHASE4AT_PRODUCTION_IDENTITY_BINDING_MISMATCH")
    if not isinstance(authorization.get("attempt_id"), str) or not authorization["attempt_id"]:
        raise ValueError("PHASE4AT_ATTEMPT_ID_INVALID")
    if now >= _time(authorization.get("effective_expiration"), "AUTHORIZATION_EXPIRATION"):
        raise ValueError("PHASE4AT_AUTHORIZATION_EXPIRED")
    if any(
        item.get("execution_authorized") is not False
        for item in (readiness, simulation, authorization)
    ):
        raise ValueError("PHASE4AT_PRODUCTION_AUTHORIZATION_PRESENT")
    _time(row.get("proposed_settled_at"), "PROPOSED_TIMESTAMP")
    connection = sqlite3.connect(sandbox_db)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        marker = connection.execute(
            "SELECT marker_schema, disposable FROM phase4al_disposable_marker WHERE id=1"
        ).fetchone()
    except sqlite3.Error as exc:
        connection.close()
        raise ValueError("PHASE4AT_DISPOSABLE_MARKER_MISSING") from exc
    if marker is None or marker["marker_schema"] != MARKER_SCHEMA or marker["disposable"] != 1:
        connection.close()
        raise ValueError("PHASE4AT_DISPOSABLE_MARKER_INVALID")
    preconditions = row.get("compare_and_swap_preconditions", {})
    before = connection.execute(
        "SELECT * FROM settlements WHERE ticker=?", (row["ticker"],)
    ).fetchone()
    if before is None or before["settled_at"] not in (None, ""):
        connection.close()
        raise ValueError("PHASE4AT_PRECONDITION_TIMESTAMP_PRESENT_OR_ROW_MISSING")
    if before["result"] != preconditions.get("result") or settlement_lineage_hash(
        dict(before)
    ) != preconditions.get("settlement_lineage_hash"):
        connection.close()
        raise ValueError("PHASE4AT_PRECONDITION_LINEAGE_CHANGED")
    affected = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT * FROM settlements WHERE ticker=?", (row["ticker"],)
        ).fetchone()
        if current is None or settlement_lineage_hash(dict(current)) != preconditions.get(
            "settlement_lineage_hash"
        ):
            raise ValueError("PHASE4AT_IN_TRANSACTION_REVALIDATION_FAILED")
        cursor = connection.execute(
            "UPDATE settlements SET settled_at=? WHERE ticker=? AND settled_at IS NULL "
            "AND result=? AND updated_at IS ?",
            (
                row["proposed_settled_at"],
                row["ticker"],
                preconditions["result"],
                preconditions["updated_at"],
            ),
        )
        affected = cursor.rowcount
        if affected != 1:
            raise ValueError("PHASE4AT_COMPARE_AND_SWAP_ROW_COUNT_INVALID")
        after = connection.execute(
            "SELECT * FROM settlements WHERE ticker=?", (row["ticker"],)
        ).fetchone()
        if after is None or after["settled_at"] != row["proposed_settled_at"]:
            raise ValueError("PHASE4AT_POSTCONDITION_FAILED")
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise
    connection.close()
    post_isolation = _validate_identity(sandbox_db, protected)
    if post_isolation != isolation:
        raise ValueError("PHASE4AT_PROTECTED_IDENTITY_CHANGED_DURING_ATTEMPT")
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AT",
        "operation": OPERATION,
        "evaluated_at": now.isoformat(),
        "attempt_id": authorization.get("attempt_id"),
        "ticker": row["ticker"],
        "readiness_row_hash": row["readiness_row_hash"],
        "input_hashes": {
            "phase4ak_readiness": readiness["artifact_hash"],
            "phase4al_simulation": simulation["artifact_hash"],
            "phase4aq_validation": authorization["artifact_hash"],
            "protected_identities": protected["artifact_hash"],
            "executor_build_identity": executor_build_identity_hash,
        },
        "isolation_proof": isolation,
        "post_commit_isolation_proof": post_isolation,
        "before_row_hash": canonical_hash(dict(before)),
        "after_row_hash": canonical_hash(dict(after)),
        "affected_row_count": affected,
        "simulation_state": "SANDBOX_SIMULATION_COMMITTED",
        "production_database_mutated": False,
        "research_database_mutated": False,
        "database_mutation_performed": False,
        "disposable_simulation_mutation_performed": True,
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    pair_id = canonical_hash(
        {"attempt": receipt["attempt_id"], "row": receipt["after_row_hash"], "operation": OPERATION}
    )
    receipt["publication_pair_id"] = pair_id
    receipt["artifact_hash"] = _hash(receipt)
    proof: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "phase": "4AT",
        "publication_pair_id": pair_id,
        "receipt_hash": receipt["artifact_hash"],
        "single_operation_only": True,
        "operation": OPERATION,
        "protected_identities_refused": True,
        "disposable_marker_verified": True,
        "transactional_compare_and_swap_verified": True,
        "production_path_override_present": False,
        "runtime_service_integration_present": False,
        "production_execution_authorized": False,
    }
    proof["manifest_hash"] = _hash(proof, "manifest_hash")
    return receipt, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-db", type=Path, required=True)
    parser.add_argument("--protected-identities", type=Path, required=True)
    parser.add_argument("--phase4ak-readiness", type=Path, required=True)
    parser.add_argument("--phase4al-simulation", type=Path, required=True)
    parser.add_argument("--phase4aq-validation", type=Path, required=True)
    parser.add_argument("--executor-build-identity-hash", required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    receipt, proof = build(
        args.sandbox_db,
        args.protected_identities,
        args.phase4ak_readiness,
        args.phase4al_simulation,
        args.phase4aq_validation,
        executor_build_identity_hash=args.executor_build_identity_hash,
        now=_time(args.evaluation_time, "EVALUATION_TIME"),
    )
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.receipt_output, args.proof_output, receipt, proof)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
