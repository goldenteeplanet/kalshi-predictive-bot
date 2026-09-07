"""Phase 4AV disposable-only rollback and pre-mutation refusal certification matrix."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4av.rollback-certification-matrix.v1"
PROOF_SCHEMA = "phase4av.rollback-proof.v1"
MARKER_SCHEMA = "phase4al.disposable-simulation-database.v1"
SCENARIOS = (
    "INPUT_FAILURE",
    "AUTHORIZATION_FAILURE",
    "EXPIRATION_FAILURE",
    "PATH_ISOLATION_FAILURE",
    "DATABASE_CONTENTION",
    "ZERO_ROW_UPDATE",
    "MULTI_ROW_UPDATE",
    "POSTCONDITION_FAILURE",
    "ARTIFACT_PUBLICATION_FAILURE",
    "SIMULATED_CRASH_AFTER_BEGIN",
    "SIMULATED_CRASH_AFTER_UPDATE",
)


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _snapshot(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path.resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        state: dict[str, Any] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
            order = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
            state[table] = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {quoted} ORDER BY {order}"  # noqa: S608
                )
            ]
        return canonical_hash(state)
    finally:
        connection.close()


def _validate_template(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.resolve(strict=True)}?mode=ro", uri=True)
    try:
        marker = connection.execute(
            "SELECT marker_schema,disposable FROM phase4al_disposable_marker WHERE id=1"
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) FROM settlements WHERE settled_at IS NULL"
        ).fetchone()[0]
    except sqlite3.Error as exc:
        raise ValueError("PHASE4AV_TEMPLATE_INVALID") from exc
    finally:
        connection.close()
    if marker != (MARKER_SCHEMA, 1) or count < 2:
        raise ValueError("PHASE4AV_TEMPLATE_NOT_DISPOSABLE_OR_INSUFFICIENT_ROWS")


def _attempt(path: Path, scenario: str) -> tuple[str, str]:
    before = _snapshot(path)
    if scenario in {
        "INPUT_FAILURE",
        "AUTHORIZATION_FAILURE",
        "EXPIRATION_FAILURE",
        "PATH_ISOLATION_FAILURE",
        "ARTIFACT_PUBLICATION_FAILURE",
    }:
        return "PRE_MUTATION_REFUSAL", before
    if scenario == "DATABASE_CONTENTION":
        locker = sqlite3.connect(path, timeout=0)
        contender = sqlite3.connect(path, timeout=0)
        try:
            locker.execute("BEGIN IMMEDIATE")
            try:
                contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                return "PRE_MUTATION_REFUSAL", _snapshot(path)
            raise ValueError("PHASE4AV_CONTENTION_NOT_ENFORCED")
        finally:
            contender.close()
            locker.rollback()
            locker.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if scenario == "ZERO_ROW_UPDATE":
            count = connection.execute(
                "UPDATE settlements SET settled_at=? WHERE ticker=? AND settled_at IS NULL",
                ("2026-08-25T12:00:00+00:00", "MISSING"),
            ).rowcount
            if count != 1:
                raise RuntimeError("EXPECTED_ONE_ROW")
        elif scenario == "MULTI_ROW_UPDATE":
            count = connection.execute(
                "UPDATE settlements SET settled_at=? WHERE settled_at IS NULL",
                ("2026-08-25T12:00:00+00:00",),
            ).rowcount
            if count != 1:
                raise RuntimeError("EXPECTED_ONE_ROW")
        elif scenario == "POSTCONDITION_FAILURE":
            connection.execute(
                "UPDATE settlements SET settled_at=? WHERE ticker=? AND settled_at IS NULL",
                ("2026-08-25T12:00:00+00:00", "KXROLL-1"),
            )
            raise RuntimeError("POSTCONDITION_FAILED")
        elif scenario == "SIMULATED_CRASH_AFTER_BEGIN":
            raise RuntimeError("CRASH")
        elif scenario == "SIMULATED_CRASH_AFTER_UPDATE":
            connection.execute(
                "UPDATE settlements SET settled_at=? WHERE ticker=? AND settled_at IS NULL",
                ("2026-08-25T12:00:00+00:00", "KXROLL-1"),
            )
            raise RuntimeError("CRASH")
        else:
            raise ValueError("PHASE4AV_SCENARIO_UNKNOWN")
    except RuntimeError:
        connection.rollback()
    finally:
        connection.close()
    after = _snapshot(path)
    if after != before:
        raise ValueError("PHASE4AV_ROLLBACK_STATE_CHANGED")
    return "TRANSACTION_ROLLED_BACK", after


def build(
    template_database: Path, *, now: datetime, work_root: Path | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AV_EVALUATION_TIMEZONE_MISSING")
    _validate_template(template_database)
    owned = tempfile.TemporaryDirectory(prefix="phase4av-") if work_root is None else None
    root = Path(owned.name) if owned else work_root
    assert root is not None
    root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    try:
        for index, scenario in enumerate(SCENARIOS):
            database = root / f"{index:02d}-{scenario.lower()}.db"
            if database.exists():
                raise FileExistsError("PHASE4AV_DISPOSABLE_CASE_EXISTS")
            shutil.copy2(template_database, database)
            baseline = _snapshot(database)
            outcome, final = _attempt(database, scenario)
            if baseline != final:
                raise ValueError("PHASE4AV_CASE_NOT_PRESERVED")
            row = {
                "scenario": scenario,
                "outcome": outcome,
                "baseline_state_hash": baseline,
                "final_state_hash": final,
                "state_preserved": True,
                "production_database_mutated": False,
                "execution_authorized": False,
            }
            row["row_hash"] = canonical_hash(row)
            rows.append(row)
    finally:
        if owned is not None:
            owned.cleanup()
    evaluated_at = now.astimezone(UTC).isoformat()
    matrix: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AV",
        "evaluated_at": evaluated_at,
        "scenario_count": len(rows),
        "rows": rows,
        "all_cases_refused_or_rolled_back": all(row["state_preserved"] for row in rows),
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    matrix["artifact_hash"] = _hash(matrix)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4AV",
        "evaluated_at": evaluated_at,
        "matrix_hash": matrix["artifact_hash"],
        "scenario_row_hashes": [row["row_hash"] for row in rows],
        "rollback_certified": True,
        "production_database_mutated": False,
        "research_database_mutated": False,
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return matrix, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-template-db", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--matrix-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    matrix, proof = build(args.disposable_template_db, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.matrix_output, args.proof_output, matrix, proof)
    print(json.dumps(matrix, sort_keys=True))


if __name__ == "__main__":
    main()
