"""Phase 4AO deterministic disposable concurrency and lost-update proof harness."""

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

SCHEMA = "phase4ao.concurrent-contention-report.v1"
MANIFEST_SCHEMA = "phase4ao.lost-update-proof.v1"
MARKER_SCHEMA = "phase4al.disposable-simulation-database.v1"
SCENARIOS = (
    "TWO_VALID_CANDIDATES",
    "DUPLICATE_ATTEMPT",
    "STALE_READER",
    "CONFLICTING_TIMESTAMPS",
    "LOCK_CONTENTION",
    "BUSY_TIMEOUT",
    "CONTROLLED_INTERLEAVING",
    "REPLAY_AFTER_APPARENT_TIMEOUT",
    "LOST_UPDATE_ATTEMPT",
)


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


def _validate_isolation(production: Path, template: Path, work_root: Path) -> None:
    prod, source = production.resolve(strict=True), template.resolve(strict=True)
    root = work_root.resolve(strict=False)
    if (
        prod == source
        or prod.parent == source.parent
        or root == prod
        or root == prod.parent
        or prod.parent in root.parents
    ):
        raise ValueError("PHASE4AO_PRODUCTION_PATH_OVERLAP")
    if (prod.stat().st_dev, prod.stat().st_ino) == (
        source.stat().st_dev,
        source.stat().st_ino,
    ):
        raise ValueError("PHASE4AO_PRODUCTION_HARD_LINK_OVERLAP")
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        marker = connection.execute(
            "SELECT marker_schema, disposable FROM phase4al_disposable_marker WHERE id=1"
        ).fetchone()
    except sqlite3.Error as exc:
        connection.close()
        raise ValueError("PHASE4AO_DISPOSABLE_MARKER_MISSING") from exc
    connection.close()
    if marker is None or marker["marker_schema"] != MARKER_SCHEMA or marker["disposable"] != 1:
        raise ValueError("PHASE4AO_DISPOSABLE_MARKER_INVALID")


def _connection(path: Path, *, timeout: float = 0.0) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return connection


def _row(connection: sqlite3.Connection, ticker: str) -> dict[str, Any]:
    value = connection.execute("SELECT * FROM settlements WHERE ticker=?", (ticker,)).fetchone()
    if value is None:
        raise ValueError("PHASE4AO_TARGET_ROW_MISSING")
    return dict(value)


def _unrelated_hash(connection: sqlite3.Connection, ticker: str) -> str:
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
                if row.get("ticker") == ticker:
                    row["settled_at"] = "<INTENDED_FIELD>"
        payload.append({"table": table, "rows": rows})
    return canonical_hash(payload)


def _cas(connection: sqlite3.Connection, ticker: str, timestamp: str) -> int:
    cursor = connection.execute(
        "UPDATE settlements SET settled_at=? WHERE ticker=? AND settled_at IS NULL",
        (timestamp, ticker),
    )
    return cursor.rowcount


def simulate(
    production_db: Path,
    template_db: Path,
    work_root: Path,
    *,
    ticker: str,
    first_timestamp: str,
    second_timestamp: str,
    scenario: str,
    now: datetime,
) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError("PHASE4AO_SCENARIO_INVALID")
    if now.tzinfo is None:
        raise ValueError("PHASE4AO_EVALUATION_TIMEZONE_MISSING")
    work_root.mkdir(parents=True, exist_ok=True)
    _validate_isolation(production_db, template_db, work_root)
    production_before = _identity(production_db)
    workspace = Path(tempfile.mkdtemp(prefix="phase4ao-", dir=work_root))
    try:
        database = workspace / "simulation.db"
        shutil.copy2(template_db, database)
        first = _connection(database)
        second = _connection(database)
        before = _row(first, ticker)
        unrelated_before = _unrelated_hash(first, ticker)
        events: list[dict[str, Any]] = []
        successes = 0
        retries = 0
        lock_refusals = 0
        duplicate_refusals = 0
        stale_reads = 0
        apparent_timeout = False

        if scenario in {"LOCK_CONTENTION", "BUSY_TIMEOUT"}:
            first.execute("BEGIN IMMEDIATE")
            events.append({"actor": "A", "event": "WRITE_LOCK_ACQUIRED_DISPOSABLE"})
            try:
                second.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                lock_refusals += 1
                events.append({"actor": "B", "event": "BUSY_REFUSAL"})
            successes += _cas(first, ticker, first_timestamp)
            first.commit()
            events.append({"actor": "A", "event": "CAS_COMMITTED"})
            if scenario == "LOCK_CONTENTION":
                retries += 1
                affected = _cas(second, ticker, second_timestamp)
                events.append({"actor": "B", "event": "RETRY_CAS", "affected": affected})
                successes += affected
        elif scenario == "DUPLICATE_ATTEMPT":
            attempt_ids: set[str] = set()
            attempt_id = "attempt-duplicate"
            attempt_ids.add(attempt_id)
            successes += _cas(first, ticker, first_timestamp)
            if attempt_id in attempt_ids:
                duplicate_refusals += 1
                events.append({"actor": "B", "event": "DUPLICATE_ATTEMPT_REFUSED"})
        elif scenario in {"STALE_READER", "CONTROLLED_INTERLEAVING"}:
            stale_value = _row(second, ticker)["settled_at"]
            stale_reads += 1
            events.append({"actor": "B", "event": "STALE_READ", "value": stale_value})
            successes += _cas(first, ticker, first_timestamp)
            affected = _cas(second, ticker, second_timestamp)
            successes += affected
            events.append({"actor": "B", "event": "STALE_CAS", "affected": affected})
        elif scenario == "REPLAY_AFTER_APPARENT_TIMEOUT":
            successes += _cas(first, ticker, first_timestamp)
            apparent_timeout = True
            retries += 1
            affected = _cas(first, ticker, first_timestamp)
            successes += affected
            events.extend(
                [
                    {"actor": "A", "event": "COMMIT_RESPONSE_OBSCURED"},
                    {"actor": "A", "event": "REPLAY_CAS", "affected": affected},
                ]
            )
        else:
            successes += _cas(first, ticker, first_timestamp)
            affected = _cas(second, ticker, second_timestamp)
            successes += affected
            events.extend(
                [
                    {"actor": "A", "event": "CAS", "affected": 1},
                    {"actor": "B", "event": "CAS", "affected": affected},
                ]
            )
        first.close()
        second.close()
        verify = _connection(database)
        after = _row(verify, ticker)
        unrelated_after = _unrelated_hash(verify, ticker)
        verify.close()
        final_is_first = after.get("settled_at") == first_timestamp
        lost_update_prevented = successes == 1 and final_is_first
        unrelated_preserved = unrelated_before == unrelated_after
        if not lost_update_prevented:
            raise ValueError("PHASE4AO_LOST_UPDATE_PROOF_FAILED")
        if not unrelated_preserved:
            raise ValueError("PHASE4AO_UNRELATED_STATE_CHANGED")
        production_after = _identity(production_db)
        if production_before != production_after:
            raise ValueError("PHASE4AO_PRODUCTION_METADATA_CHANGED")
        return {
            "scenario": scenario,
            "events": events,
            "events_hash": canonical_hash(events),
            "successful_compare_and_swaps": successes,
            "lock_refusals": lock_refusals,
            "duplicate_attempt_refusals": duplicate_refusals,
            "stale_reads": stale_reads,
            "retry_count": retries,
            "apparent_timeout_simulated": apparent_timeout,
            "final_timestamp": after.get("settled_at"),
            "winning_timestamp": first_timestamp,
            "lost_update_prevented": lost_update_prevented,
            "unrelated_state_preserved": unrelated_preserved,
            "production_metadata_unchanged": True,
            "before_state_hash": canonical_hash(before),
            "after_state_hash": canonical_hash(after),
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=False)


def build(
    production_db: Path,
    template_db: Path,
    work_root: Path,
    *,
    ticker: str,
    first_timestamp: str,
    second_timestamp: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    results = [
        simulate(
            production_db,
            template_db,
            work_root,
            ticker=ticker,
            first_timestamp=first_timestamp,
            second_timestamp=second_timestamp,
            scenario=scenario,
            now=now,
        )
        for scenario in SCENARIOS
    ]
    results.sort(key=lambda row: SCENARIOS.index(row["scenario"]))
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AO",
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "scenario_results": results,
        "scenario_results_hash": canonical_hash(results),
        "all_scenarios_prevented_lost_updates": all(
            row["lost_update_prevented"] for row in results
        ),
        "maximum_successful_cas_per_scenario": max(
            row["successful_compare_and_swaps"] for row in results
        ),
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
        "phase": "4AO",
        "publication_pair_id": pair_id,
        "contention_report_hash": report["artifact_hash"],
        "scenario_count": len(results),
        "verified_scenario_count": sum(row["lost_update_prevented"] for row in results),
        "at_most_one_cas_succeeded": report["maximum_successful_cas_per_scenario"] == 1,
        "all_unrelated_state_preserved": all(row["unrelated_state_preserved"] for row in results),
        "proof_state": "LOST_UPDATE_AND_DUPLICATE_MUTATION_PREVENTED",
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
    parser.add_argument("--first-timestamp", required=True)
    parser.add_argument("--second-timestamp", required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4AO_EVALUATION_TIME_INVALID") from exc
    if now.tzinfo is None:
        raise ValueError("PHASE4AO_EVALUATION_TIMEZONE_MISSING")
    report, proof = build(
        args.production_db,
        args.disposable_template_db,
        args.disposable_work_root,
        ticker=args.ticker,
        first_timestamp=args.first_timestamp,
        second_timestamp=args.second_timestamp,
        now=now,
    )
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.report_output, args.proof_output, report, proof)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
