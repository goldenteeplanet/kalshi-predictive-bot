"""Phase 4AN disposable crash-consistency and recovery simulator."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4an.crash-consistency-report.v1"
MANIFEST_SCHEMA = "phase4an.recovery-proof-manifest.v1"
RECEIPT_SCHEMA = "phase4an.disposable-simulation-receipt.v1"
RECEIPT_PROOF_SCHEMA = "phase4an.disposable-receipt-proof.v1"
MARKER_SCHEMA = "phase4al.disposable-simulation-database.v1"
CRASH_STAGES = (
    "BEFORE_TRANSACTION",
    "DURING_TRANSACTION",
    "AFTER_MUTATION_BEFORE_COMMIT",
    "AFTER_DATABASE_COMMIT_BEFORE_RECEIPT",
    "DURING_ARTIFACT_PUBLICATION",
    "BETWEEN_PAIRED_REPLACEMENTS",
    "DURING_BACKUP_RESTORATION",
)


class SimulatedCrash(RuntimeError):
    """Deterministic process-termination surrogate used only in disposable workspaces."""


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


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


def _validate_template(production: Path, template: Path, work_root: Path) -> None:
    prod = production.resolve(strict=True)
    source = template.resolve(strict=True)
    root = work_root.resolve(strict=False)
    if (
        source == prod
        or source.parent == prod.parent
        or root == prod.parent
        or prod.parent in root.parents
        or root == prod
    ):
        raise ValueError("PHASE4AN_PRODUCTION_PATH_OVERLAP")
    prod_stat, source_stat = prod.stat(), source.stat()
    if (prod_stat.st_dev, prod_stat.st_ino) == (source_stat.st_dev, source_stat.st_ino):
        raise ValueError("PHASE4AN_PRODUCTION_HARD_LINK_OVERLAP")
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        marker = connection.execute(
            "SELECT marker_schema, disposable FROM phase4al_disposable_marker WHERE id=1"
        ).fetchone()
    except sqlite3.Error as exc:
        connection.close()
        raise ValueError("PHASE4AN_DISPOSABLE_MARKER_MISSING") from exc
    connection.close()
    if marker is None or marker["marker_schema"] != MARKER_SCHEMA or marker["disposable"] != 1:
        raise ValueError("PHASE4AN_DISPOSABLE_MARKER_INVALID")


def _state(connection: sqlite3.Connection, ticker: str) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT * FROM settlements WHERE ticker=?", (ticker,)).fetchone()
    if row is None:
        raise ValueError("PHASE4AN_TARGET_ROW_MISSING")
    return dict(row)


def _receipt(operation_id: str, ticker: str, before: dict[str, Any], after: dict[str, Any]):
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "operation_id": operation_id,
        "ticker": ticker,
        "before_hash": canonical_hash(before),
        "after_hash": canonical_hash(after),
        "disposable_simulation_only": True,
        "production_execution_authorized": False,
    }
    pair_id = canonical_hash({"operation_id": operation_id, "after": receipt["after_hash"]})
    receipt["publication_pair_id"] = pair_id
    receipt["artifact_hash"] = _hash(receipt)
    proof: dict[str, Any] = {
        "schema": RECEIPT_PROOF_SCHEMA,
        "publication_pair_id": pair_id,
        "receipt_hash": receipt["artifact_hash"],
        "recovered_from_disposable_state": True,
        "production_execution_authorized": False,
    }
    proof["manifest_hash"] = _hash(proof, "manifest_hash")
    return receipt, proof


def _write_durable(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _publish_receipt(
    directory: Path,
    receipt: dict[str, Any],
    proof: dict[str, Any],
    crash_stage: str | None = None,
) -> None:
    finals = directory / "receipt.json", directory / "receipt-proof.json"
    temporary = directory / ".receipt.json.tmp", directory / ".receipt-proof.json.tmp"
    backups = directory / ".receipt.json.bak", directory / ".receipt-proof.json.bak"
    for path, payload in zip(temporary, (receipt, proof), strict=True):
        _write_durable(path, payload)
        if crash_stage == "DURING_ARTIFACT_PUBLICATION":
            raise SimulatedCrash("PHASE4AN_CRASH_DURING_ARTIFACT_PUBLICATION")
    if crash_stage == "DURING_BACKUP_RESTORATION":
        old_receipt, old_proof = _receipt("superseded-operation", "OLD", {}, {})
        _write_durable(finals[0], old_receipt)
        _write_durable(finals[1], old_proof)
        os.replace(finals[0], backups[0])
        os.replace(finals[1], backups[1])
    os.replace(temporary[0], finals[0])
    if crash_stage in {"BETWEEN_PAIRED_REPLACEMENTS", "DURING_BACKUP_RESTORATION"}:
        raise SimulatedCrash(f"PHASE4AN_CRASH_{crash_stage}")
    os.replace(temporary[1], finals[1])


def _pair_valid(directory: Path) -> bool:
    paths = directory / "receipt.json", directory / "receipt-proof.json"
    if not all(path.is_file() for path in paths):
        return False
    try:
        receipt, proof = (json.loads(path.read_text(encoding="utf-8")) for path in paths)
    except (OSError, json.JSONDecodeError):
        return False
    return (
        receipt.get("schema") == RECEIPT_SCHEMA
        and proof.get("schema") == RECEIPT_PROOF_SCHEMA
        and receipt.get("artifact_hash") == _hash(receipt)
        and proof.get("manifest_hash") == _hash(proof, "manifest_hash")
        and receipt.get("publication_pair_id") == proof.get("publication_pair_id")
        and proof.get("receipt_hash") == receipt.get("artifact_hash")
    )


def _clean_publication_state(directory: Path) -> None:
    for name in (
        "receipt.json",
        "receipt-proof.json",
        ".receipt.json.tmp",
        ".receipt-proof.json.tmp",
        ".receipt.json.bak",
        ".receipt-proof.json.bak",
    ):
        path = directory / name
        if path.exists():
            path.unlink()


def simulate(
    production_db: Path,
    template_db: Path,
    work_root: Path,
    *,
    ticker: str,
    proposed_settled_at: str,
    crash_stage: str,
    now: datetime,
) -> dict[str, Any]:
    if crash_stage not in CRASH_STAGES:
        raise ValueError("PHASE4AN_CRASH_STAGE_INVALID")
    if now.tzinfo is None:
        raise ValueError("PHASE4AN_EVALUATION_TIMEZONE_MISSING")
    work_root.mkdir(parents=True, exist_ok=True)
    _validate_template(production_db, template_db, work_root)
    production_before = _identity(production_db)
    workspace = Path(tempfile.mkdtemp(prefix="phase4an-", dir=work_root))
    try:
        simulation_db = workspace / "simulation.db"
        shutil.copy2(template_db, simulation_db)
        connection = sqlite3.connect(simulation_db)
        before = _state(connection, ticker)
        if before.get("settled_at") not in (None, ""):
            connection.close()
            raise ValueError("PHASE4AN_TARGET_ALREADY_SETTLED")
        expected_after = dict(before)
        expected_after["settled_at"] = proposed_settled_at
        operation_id = canonical_hash(
            {
                "ticker": ticker,
                "before": canonical_hash(before),
                "proposed": proposed_settled_at,
                "evaluated_at": now.astimezone(UTC).isoformat(),
            }
        )
        crashed = False
        try:
            if crash_stage == "BEFORE_TRANSACTION":
                raise SimulatedCrash("PHASE4AN_CRASH_BEFORE_TRANSACTION")
            connection.execute("BEGIN IMMEDIATE")
            if crash_stage == "DURING_TRANSACTION":
                raise SimulatedCrash("PHASE4AN_CRASH_DURING_TRANSACTION")
            cursor = connection.execute(
                "UPDATE settlements SET settled_at=? WHERE ticker=? AND settled_at IS NULL",
                (proposed_settled_at, ticker),
            )
            if cursor.rowcount != 1:
                raise ValueError("PHASE4AN_COMPARE_AND_SWAP_FAILED")
            if crash_stage == "AFTER_MUTATION_BEFORE_COMMIT":
                raise SimulatedCrash("PHASE4AN_CRASH_AFTER_MUTATION_BEFORE_COMMIT")
            connection.commit()
            if crash_stage == "AFTER_DATABASE_COMMIT_BEFORE_RECEIPT":
                raise SimulatedCrash("PHASE4AN_CRASH_AFTER_DATABASE_COMMIT_BEFORE_RECEIPT")
            receipt, proof = _receipt(operation_id, ticker, before, expected_after)
            _publish_receipt(workspace, receipt, proof, crash_stage)
        except SimulatedCrash:
            crashed = True
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        finally:
            connection.close()

        reopened = sqlite3.connect(simulation_db)
        observed = _state(reopened, ticker)
        reopened.close()
        database_state = (
            "PRE_TRANSACTION_STATE"
            if observed == before
            else "COMMITTED_STATE"
            if observed == expected_after
            else "UNCLASSIFIABLE_STATE"
        )
        pair_before_recovery = (
            "VALID_PAIR"
            if _pair_valid(workspace)
            else "ABSENT"
            if not any(workspace.glob("*receipt*"))
            else "PARTIAL_OR_INVALID"
        )
        if database_state == "UNCLASSIFIABLE_STATE":
            raise ValueError("PHASE4AN_DATABASE_STATE_UNCLASSIFIABLE")
        _clean_publication_state(workspace)
        if database_state == "COMMITTED_STATE":
            receipt, proof = _receipt(operation_id, ticker, before, observed)
            _publish_receipt(workspace, receipt, proof)
            recovery_state = "COMMIT_AND_RECEIPT_RECOVERED"
        else:
            recovery_state = "ROLLBACK_OR_PREMUTATION_REFUSAL_RECOVERED"
        pair_after_recovery = "VALID_PAIR" if _pair_valid(workspace) else "ABSENT"
        recovery_valid = (
            database_state == "COMMITTED_STATE" and pair_after_recovery == "VALID_PAIR"
        ) or (database_state == "PRE_TRANSACTION_STATE" and pair_after_recovery == "ABSENT")
        if not recovery_valid:
            raise ValueError("PHASE4AN_RECOVERY_NOT_PROVED")
        production_after = _identity(production_db)
        if production_before != production_after:
            raise ValueError("PHASE4AN_PRODUCTION_METADATA_CHANGED")
        return {
            "crash_stage": crash_stage,
            "crash_injected": crashed,
            "database_state_after_crash": database_state,
            "artifact_state_after_crash": pair_before_recovery,
            "recovery_state": recovery_state,
            "artifact_state_after_recovery": pair_after_recovery,
            "recovery_verified": recovery_valid,
            "before_state_hash": canonical_hash(before),
            "after_state_hash": canonical_hash(observed),
            "production_metadata_unchanged": True,
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=False)


def build(
    production_db: Path,
    template_db: Path,
    work_root: Path,
    *,
    ticker: str,
    proposed_settled_at: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    results = [
        simulate(
            production_db,
            template_db,
            work_root,
            ticker=ticker,
            proposed_settled_at=proposed_settled_at,
            crash_stage=stage,
            now=now,
        )
        for stage in CRASH_STAGES
    ]
    results.sort(key=lambda row: CRASH_STAGES.index(row["crash_stage"]))
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AN",
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "crash_stages": list(CRASH_STAGES),
        "scenario_results": results,
        "scenario_results_hash": canonical_hash(results),
        "all_recoveries_verified": all(row["recovery_verified"] for row in results),
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
        {"evaluated_at": report["evaluated_at"], "results": report["scenario_results_hash"]}
    )
    report["publication_pair_id"] = pair_id
    report["artifact_hash"] = _hash(report)
    proof: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "phase": "4AN",
        "publication_pair_id": pair_id,
        "crash_report_hash": report["artifact_hash"],
        "verified_stage_count": sum(row["recovery_verified"] for row in results),
        "required_stage_count": len(CRASH_STAGES),
        "recovery_proof_state": "ALL_PERSISTENCE_BOUNDARIES_RECOVERABLE"
        if report["all_recoveries_verified"]
        else "RECOVERY_INCOMPLETE",
        "production_recovery_commands_included": False,
        "production_execution_authorized": False,
    }
    proof["manifest_hash"] = _hash(proof, "manifest_hash")
    return report, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--disposable-template-db", type=Path, required=True)
    parser.add_argument("--disposable-work-root", type=Path, required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--proposed-settled-at", required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4AN_EVALUATION_TIME_INVALID") from exc
    if now.tzinfo is None:
        raise ValueError("PHASE4AN_EVALUATION_TIMEZONE_MISSING")
    report, proof = build(
        args.production_db,
        args.disposable_template_db,
        args.disposable_work_root,
        ticker=args.ticker,
        proposed_settled_at=args.proposed_settled_at,
        now=now,
    )
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.report_output, args.proof_output, report, proof)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
