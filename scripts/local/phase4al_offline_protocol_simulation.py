"""Phase 4AL isolated offline settlement protocol simulator."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash, settlement_lineage_hash

SCHEMA = "phase4al.offline-protocol-simulation-report.v1"
MANIFEST_SCHEMA = "phase4al.protocol-safety-proof.v1"
AK_SCHEMA = "phase4ak.settlement-mutation-readiness-envelope.v1"
AK_MANIFEST_SCHEMA = "phase4ak.executor-design-handoff.v1"
MARKER_SCHEMA = "phase4al.disposable-simulation-database.v1"
FAILURE_STAGES = (
    "BEFORE_TRANSACTION",
    "AFTER_INITIAL_PRECONDITION",
    "AFTER_TRANSACTION_START",
    "AFTER_IN_TRANSACTION_REVALIDATION",
    "BEFORE_SIMULATED_MUTATION",
    "AFTER_SIMULATED_MUTATION",
    "BEFORE_ROW_COUNT_VERIFICATION",
    "BEFORE_POSTCONDITION_VERIFICATION",
    "IMMEDIATELY_BEFORE_COMMIT",
)


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load_rows(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AL_READINESS_UNREADABLE") from exc
    if payload.get("schema") != AK_SCHEMA:
        raise ValueError("PHASE4AL_READINESS_SCHEMA_INVALID")
    if payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4AL_READINESS_HASH_MISMATCH")
    if not isinstance(payload.get("rows"), list) or payload.get("rows_hash") != canonical_hash(
        payload["rows"]
    ):
        raise ValueError("PHASE4AL_READINESS_ROWS_HASH_MISMATCH")
    return payload


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AL_HANDOFF_UNREADABLE") from exc
    if payload.get("schema") != AK_MANIFEST_SCHEMA:
        raise ValueError("PHASE4AL_HANDOFF_SCHEMA_INVALID")
    if payload.get("manifest_hash") != _hash(payload, "manifest_hash"):
        raise ValueError("PHASE4AL_HANDOFF_HASH_MISMATCH")
    return payload


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"PHASE4AL_{label}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"PHASE4AL_{label}_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"PHASE4AL_{label}_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def _production_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("PHASE4AL_PRODUCTION_DATABASE_NOT_FILE")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ValueError("PHASE4AL_PRODUCTION_QUERY_ONLY_NOT_ENFORCED")
    return connection


def _identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _assert_production_identity(
    production_db: Path, readiness_identity: Any, observed: dict[str, Any]
) -> None:
    if not isinstance(readiness_identity, dict):
        raise ValueError("PHASE4AL_PRODUCTION_IDENTITY_MISSING")
    try:
        expected_path = str(Path(readiness_identity["path"]).resolve(strict=True))
        expected_size = int(readiness_identity["size"])
        expected_mtime_ns = int(readiness_identity["mtime_ns"])
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError("PHASE4AL_PRODUCTION_IDENTITY_INVALID") from exc
    if expected_path != str(production_db.resolve(strict=True)) or (
        expected_size,
        expected_mtime_ns,
    ) != (observed["size"], observed["mtime_ns"]):
        raise ValueError("PHASE4AL_PRODUCTION_IDENTITY_MISMATCH")


def _disposable_identity(path: Path, initial_state_hash: str) -> dict[str, Any]:
    """Return only stable identity fields so identical starting states reproduce exactly."""
    observed = _identity(path)
    return {
        "path": observed["path"],
        "device": observed["device"],
        "inode": observed["inode"],
        "initial_logical_state_hash": initial_state_hash,
    }


def _open_disposable(
    production: Path, simulation: Path
) -> tuple[sqlite3.Connection, dict[str, Any]]:
    if not simulation.is_file():
        raise ValueError("PHASE4AL_SIMULATION_DATABASE_NOT_FILE")
    prod = production.resolve(strict=True)
    sim = simulation.resolve(strict=True)
    if prod == sim:
        raise ValueError("PHASE4AL_PATH_IDENTITY_EQUAL")
    if prod.parent == sim.parent:
        raise ValueError("PHASE4AL_SIMULATION_IN_PROTECTED_DIRECTORY")
    prod_stat, sim_stat = prod.stat(), sim.stat()
    if (prod_stat.st_dev, prod_stat.st_ino) == (sim_stat.st_dev, sim_stat.st_ino):
        raise ValueError("PHASE4AL_PATH_IDENTITY_HARD_LINK")
    connection = sqlite3.connect(sim)
    connection.row_factory = sqlite3.Row
    try:
        marker = connection.execute(
            "SELECT marker_schema, disposable FROM phase4al_disposable_marker WHERE id=1"
        ).fetchone()
    except sqlite3.Error as exc:
        connection.close()
        raise ValueError("PHASE4AL_DISPOSABLE_MARKER_MISSING") from exc
    if marker is None or marker["marker_schema"] != MARKER_SCHEMA or marker["disposable"] != 1:
        connection.close()
        raise ValueError("PHASE4AL_DISPOSABLE_MARKER_INVALID")
    return connection, {
        "production_resolved_path": str(prod),
        "simulation_resolved_path": str(sim),
        "paths_distinct": True,
        "parents_distinct": True,
        "device_inode_distinct": True,
        "disposable_marker_valid": True,
    }


def _logical_snapshot(connection: sqlite3.Connection) -> str:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    payload: list[dict[str, Any]] = []
    for table in tables:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        payload.append({"table": table, "columns": columns, "rows": rows})
    return canonical_hash(payload)


def _preservation_snapshot(connection: sqlite3.Connection, tickers: set[str]) -> str:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    payload: list[dict[str, Any]] = []
    for table in tables:
        rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        if table == "settlements":
            for row in rows:
                if str(row.get("ticker")) in tickers:
                    row["settled_at"] = "<INTENDED_FIELD>"
        payload.append({"table": table, "rows": rows})
    return canonical_hash(payload)


def _inject(stage: str | None, current: str) -> None:
    if stage == current:
        raise RuntimeError(f"PHASE4AL_INJECTED_FAILURE_{current}")


def build(
    production_db: Path,
    simulation_db: Path,
    readiness_path: Path,
    handoff_path: Path,
    *,
    now: datetime,
    failure_stage: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AL_EVALUATION_TIMEZONE_MISSING")
    if failure_stage is not None and failure_stage not in FAILURE_STAGES:
        raise ValueError("PHASE4AL_FAILURE_STAGE_INVALID")
    now = now.astimezone(UTC)
    readiness = _load_rows(readiness_path)
    handoff = _load_manifest(handoff_path)
    if readiness.get("publication_pair_id") != handoff.get("publication_pair_id"):
        raise ValueError("PHASE4AL_LINEAGE_PAIR_MISMATCH")
    if handoff.get("readiness_envelope_hash") != readiness.get("artifact_hash"):
        raise ValueError("PHASE4AL_LINEAGE_ENVELOPE_MISMATCH")
    if (
        any(item.get("execution_authorized") is not False for item in (readiness, handoff))
        or handoff.get("executor_implemented") is not False
    ):
        raise ValueError("PHASE4AL_INPUT_AUTHORIZATION_PRESENT")
    production_before = _identity(production_db)
    _assert_production_identity(
        production_db, readiness.get("production_database_identity"), production_before
    )
    production = _production_ro(production_db)
    try:
        production.execute("SELECT 1").fetchone()
    finally:
        production.close()
    simulation, isolation = _open_disposable(production_db, simulation_db)
    candidates = sorted(
        (row for row in readiness["rows"] if row.get("readiness_result") == "READY"),
        key=lambda row: str(row.get("ticker")),
    )
    if len({row.get("ticker") for row in candidates}) != len(candidates):
        simulation.close()
        raise ValueError("PHASE4AL_DUPLICATE_CANDIDATE")
    earliest = _time(handoff.get("earliest_expiration_deadline"), "EXPIRATION")
    initial_hash = _logical_snapshot(simulation)
    preservation_before = _preservation_snapshot(
        simulation, {str(row["ticker"]) for row in candidates}
    )
    stages: list[dict[str, Any]] = []
    per_row: list[dict[str, Any]] = []
    outcome = "SIMULATION_COMMITTED"
    reason_codes = ["DISPOSABLE_TRANSACTION_PROTOCOL_COMMITTED"]
    affected = 0
    rollback_verified = False
    transaction_started = False
    try:
        if readiness.get("readiness_state") != "READY_FOR_SEPARATE_EXECUTOR_DESIGN":
            outcome, reason_codes = "SIMULATION_REFUSED_NOT_READY", ["PHASE4AK_NOT_READY"]
        elif now >= earliest:
            outcome, reason_codes = "SIMULATION_REFUSED_EXPIRED", ["READINESS_EXPIRED"]
        elif not candidates:
            outcome, reason_codes = "SIMULATION_NO_ELIGIBLE_ROWS", ["NO_READY_ROWS"]
        else:
            _inject(failure_stage, "BEFORE_TRANSACTION")
            stages.append({"stage": "INITIAL_PRECONDITION", "passed": True})
            _inject(failure_stage, "AFTER_INITIAL_PRECONDITION")
            simulation.execute("BEGIN IMMEDIATE")
            transaction_started = True
            stages.append({"stage": "TRANSACTION_STARTED", "passed": True})
            _inject(failure_stage, "AFTER_TRANSACTION_START")
            for row in candidates:
                ticker = str(row["ticker"])
                current = simulation.execute(
                    "SELECT * FROM settlements WHERE ticker=?", (ticker,)
                ).fetchone()
                preconditions = row.get("compare_and_swap_preconditions", {})
                if current is None:
                    raise ValueError("PHASE4AL_PRECONDITION_SETTLEMENT_MISSING")
                if current["settled_at"] not in (None, ""):
                    raise ValueError("PHASE4AL_PRECONDITION_TIMESTAMP_PRESENT")
                if current["result"] != preconditions.get("result"):
                    raise ValueError("PHASE4AL_PRECONDITION_RESULT_CHANGED")
                if settlement_lineage_hash(dict(current)) != preconditions.get(
                    "settlement_lineage_hash"
                ):
                    raise ValueError("PHASE4AL_PRECONDITION_LINEAGE_CHANGED")
            stages.append({"stage": "IN_TRANSACTION_REVALIDATION", "passed": True})
            _inject(failure_stage, "AFTER_IN_TRANSACTION_REVALIDATION")
            _inject(failure_stage, "BEFORE_SIMULATED_MUTATION")
            for row in candidates:
                preconditions = row["compare_and_swap_preconditions"]
                cursor = simulation.execute(
                    "UPDATE settlements SET settled_at=? WHERE ticker=? "
                    "AND settled_at IS NULL AND result=? AND updated_at IS ?",
                    (
                        row["proposed_settled_at"],
                        row["ticker"],
                        preconditions["result"],
                        preconditions["updated_at"],
                    ),
                )
                affected += cursor.rowcount
                per_row.append(
                    {
                        "ticker": row["ticker"],
                        "readiness_row_hash": row["readiness_row_hash"],
                        "affected_rows": cursor.rowcount,
                        "simulation_only": True,
                    }
                )
            stages.append({"stage": "SIMULATED_MUTATION", "passed": True})
            _inject(failure_stage, "AFTER_SIMULATED_MUTATION")
            _inject(failure_stage, "BEFORE_ROW_COUNT_VERIFICATION")
            if affected != len(candidates):
                raise ArithmeticError("PHASE4AL_ROW_COUNT_MISMATCH")
            stages.append({"stage": "ROW_COUNT_VERIFIED", "passed": True})
            _inject(failure_stage, "BEFORE_POSTCONDITION_VERIFICATION")
            for row in candidates:
                settled_at = simulation.execute(
                    "SELECT settled_at FROM settlements WHERE ticker=?", (row["ticker"],)
                ).fetchone()[0]
                if settled_at != row["proposed_settled_at"]:
                    raise AssertionError("PHASE4AL_POSTCONDITION_MISMATCH")
            preservation_after = _preservation_snapshot(
                simulation, {str(row["ticker"]) for row in candidates}
            )
            if preservation_after != preservation_before:
                raise AssertionError("PHASE4AL_UNRELATED_STATE_CHANGED")
            stages.append({"stage": "POSTCONDITION_VERIFIED", "passed": True})
            _inject(failure_stage, "IMMEDIATELY_BEFORE_COMMIT")
            simulation.commit()
            transaction_started = False
            stages.append({"stage": "TRANSACTION_COMMITTED", "passed": True})
    except (RuntimeError, ValueError, ArithmeticError, AssertionError) as exc:
        if transaction_started:
            simulation.rollback()
            transaction_started = False
        final_after_failure = _logical_snapshot(simulation)
        rollback_verified = final_after_failure == initial_hash
        if isinstance(exc, RuntimeError) and str(exc).startswith("PHASE4AL_INJECTED_FAILURE_"):
            outcome = "SIMULATION_ROLLED_BACK_PRECONDITION"
        elif isinstance(exc, ArithmeticError):
            outcome = "SIMULATION_ROLLED_BACK_ROW_COUNT"
        elif isinstance(exc, AssertionError):
            outcome = "SIMULATION_ROLLED_BACK_POSTCONDITION"
        else:
            outcome = "SIMULATION_ROLLED_BACK_PRECONDITION"
        reason_codes = [str(exc)]
        if not rollback_verified:
            simulation.close()
            raise ValueError("PHASE4AL_ROLLBACK_NOT_VERIFIED") from exc
    final_hash = _logical_snapshot(simulation)
    preservation_final = _preservation_snapshot(
        simulation, {str(row["ticker"]) for row in candidates}
    )
    simulation.close()
    production_after = _identity(production_db)
    production_unchanged = production_before == production_after
    if not production_unchanged:
        raise ValueError("PHASE4AL_PRODUCTION_METADATA_CHANGED")
    simulation_id = canonical_hash(
        {
            "readiness": readiness["artifact_hash"],
            "evaluated_at": now.isoformat(),
            "failure_stage": failure_stage,
            "initial": initial_hash,
            "final": final_hash,
        }
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AL",
        "simulation_id": simulation_id,
        "evaluated_at": now.isoformat(),
        "simulation_outcome": outcome,
        "reason_codes": reason_codes,
        "input_hashes": {
            "phase4ak_handoff": handoff["manifest_hash"],
            "phase4ak_readiness": readiness["artifact_hash"],
        },
        "production_database_identity": production_before,
        "disposable_database_identity": _disposable_identity(simulation_db, initial_hash),
        "path_isolation_proof": isolation,
        "pre_transaction_state_hash": initial_hash,
        "transaction_stage_results": stages,
        "injected_failure_stage": failure_stage,
        "affected_row_count": affected,
        "post_transaction_state_hash": final_hash,
        "intended_field_change_proved": outcome == "SIMULATION_COMMITTED",
        "unrelated_state_preserved": preservation_final == preservation_before,
        "rollback_verified": rollback_verified,
        "commit_verified": outcome == "SIMULATION_COMMITTED",
        "rows": per_row,
        "rows_hash": canonical_hash(per_row),
        "production_database_mutated": False,
        "research_database_mutated": False,
        "database_mutation_performed": False,
        "disposable_simulation_mutation_performed": outcome == "SIMULATION_COMMITTED",
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    pair_id = canonical_hash({"simulation_id": simulation_id, "outcome": outcome})
    report["publication_pair_id"] = pair_id
    report["artifact_hash"] = _hash(report)
    verified = sorted(
        [
            "DISPOSABLE_AND_PRODUCTION_IDENTITIES_DISTINCT",
            "NO_EXCHANGE_OR_ORDER_INTERFACE",
            "NO_PRODUCTION_LOCK_ACQUIRED",
            "NO_SERVICE_CONTROL",
            "PRODUCTION_DATABASE_METADATA_UNCHANGED",
            "PUBLISHED_RECEIPT_IS_NON_AUTHORIZING",
            "UNRELATED_SIMULATION_STATE_PRESERVED",
        ]
        + (["TRANSACTION_COMMIT_VERIFIED"] if outcome == "SIMULATION_COMMITTED" else [])
        + (["TRANSACTION_ROLLBACK_VERIFIED"] if rollback_verified else [])
    )
    proof: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "publication_pair_id": pair_id,
        "simulation_report_hash": report["artifact_hash"],
        "phase4ak_readiness_hash": readiness["artifact_hash"],
        "phase4ak_handoff_hash": handoff["manifest_hash"],
        "simulated_readiness_row_hashes": sorted(row["readiness_row_hash"] for row in per_row),
        "protocol_version": "phase4al.disposable-cas.v1",
        "verified_invariants": verified,
        "failed_invariants": [],
        "earliest_expiration_deadline": earliest.isoformat(),
        "proof_state": "SIMULATION_SUCCESS"
        if outcome == "SIMULATION_COMMITTED"
        else "TRANSACTION_ROLLBACK_VERIFIED"
        if rollback_verified
        else "READINESS_NOT_ACCEPTABLE",
        "not_a_production_executor": True,
        "production_executor_implemented": False,
        "production_execution_authorized": False,
    }
    proof["manifest_hash"] = _hash(proof, "manifest_hash")
    return report, proof


def publish_pair(
    report_path: Path,
    proof_path: Path,
    report: dict[str, Any],
    proof: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    if report_path.parent != proof_path.parent:
        raise ValueError("PHASE4AL_OUTPUT_DIRECTORIES_DIFFER")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and (report_path.exists() or proof_path.exists()):
        raise FileExistsError("PHASE4AL_OUTPUT_EXISTS")
    temporary = [
        report_path.with_name(f".{report_path.name}.{os.getpid()}.tmp"),
        proof_path.with_name(f".{proof_path.name}.{os.getpid()}.tmp"),
    ]
    backups = [
        report_path.with_name(f".{report_path.name}.{os.getpid()}.bak"),
        proof_path.with_name(f".{proof_path.name}.{os.getpid()}.bak"),
    ]
    published: list[Path] = []
    try:
        for path, payload in zip(temporary, (report, proof), strict=True):
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        if replace:
            for final, backup in zip((report_path, proof_path), backups, strict=True):
                if final.exists():
                    os.replace(final, backup)
        for temp, final in zip(temporary, (report_path, proof_path), strict=True):
            os.replace(temp, final)
            published.append(final)
        try:
            fd = os.open(report_path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    except Exception:
        for final in published:
            if final.exists():
                final.unlink()
        for backup, final in zip(backups, (report_path, proof_path), strict=True):
            if backup.exists():
                os.replace(backup, final)
        raise
    finally:
        for path in temporary + backups:
            if path.exists():
                path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--simulation-db", type=Path, required=True)
    parser.add_argument("--phase4ak-readiness", type=Path, required=True)
    parser.add_argument("--phase4ak-handoff", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--failure-stage", choices=FAILURE_STAGES)
    parser.add_argument("--simulation-report-output", type=Path, required=True)
    parser.add_argument("--safety-proof-output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    report, proof = build(
        args.production_db,
        args.simulation_db,
        args.phase4ak_readiness,
        args.phase4ak_handoff,
        now=_time(args.evaluation_time, "EVALUATION_TIME"),
        failure_stage=args.failure_stage,
    )
    publish_pair(
        args.simulation_report_output,
        args.safety_proof_output,
        report,
        proof,
        replace=args.replace,
    )
    print(
        json.dumps({key: value for key, value in report.items() if key != "rows"}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
