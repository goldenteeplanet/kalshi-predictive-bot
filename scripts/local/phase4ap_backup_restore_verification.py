"""Phase 4AP disposable SQLite backup and distinct-target restore verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4ap.backup-verification.v1"
MANIFEST_SCHEMA = "phase4ap.restore-proof.v1"
MARKER_SCHEMA = "phase4al.disposable-simulation-database.v1"


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolved(path: Path, *, existing: bool) -> Path:
    return path.resolve(strict=existing)


def _assert_distinct(paths: dict[str, tuple[Path, bool]]) -> dict[str, str]:
    resolved = {
        name: _resolved(path, existing=existing) for name, (path, existing) in paths.items()
    }
    if len(set(resolved.values())) != len(resolved):
        raise ValueError("PHASE4AP_PATH_IDENTITY_COLLISION")
    existing = [(name, path.stat()) for name, path in resolved.items() if path.exists()]
    identities = [(name, stat.st_dev, stat.st_ino) for name, stat in existing]
    if len({(device, inode) for _, device, inode in identities}) != len(identities):
        raise ValueError("PHASE4AP_HARD_LINK_IDENTITY_COLLISION")
    protected = [resolved[name] for name in ("production", "research") if name in resolved]
    for name in ("snapshot", "restore"):
        candidate = resolved[name]
        if any(candidate == item or candidate.parent == item.parent for item in protected):
            raise ValueError("PHASE4AP_PROTECTED_RESTORE_OR_SNAPSHOT_PATH")
    return {name: str(path) for name, path in sorted(resolved.items())}


def _open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve(strict=True).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ValueError("PHASE4AP_QUERY_ONLY_NOT_ENFORCED")
    return connection


def _assert_marker(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT marker_schema, disposable FROM phase4al_disposable_marker WHERE id=1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("PHASE4AP_DISPOSABLE_MARKER_MISSING") from exc
    if row is None or row["marker_schema"] != MARKER_SCHEMA or row["disposable"] != 1:
        raise ValueError("PHASE4AP_DISPOSABLE_MARKER_INVALID")


def _catalog(connection: sqlite3.Connection) -> dict[str, Any]:
    schema = [
        dict(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]
    tables = [row["name"] for row in schema if row["type"] == "table"]
    counts: dict[str, int] = {}
    logical: list[dict[str, Any]] = []
    for table in sorted(tables):
        rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        counts[table] = len(rows)
        logical.append({"table": table, "rows": rows})
    return {
        "schema": schema,
        "schema_hash": canonical_hash(schema),
        "row_counts": dict(sorted(counts.items())),
        "row_counts_hash": canonical_hash(dict(sorted(counts.items()))),
        "logical_hash": canonical_hash(logical),
        "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
    }


def verify_snapshot(path: Path, expected_file_hash: str) -> dict[str, Any]:
    if not path.is_file() or _file_hash(path) != expected_file_hash:
        raise ValueError("PHASE4AP_SNAPSHOT_FILE_HASH_MISMATCH")
    try:
        connection = _open_ro(path)
        _assert_marker(connection)
        catalog = _catalog(connection)
        connection.close()
    except sqlite3.Error as exc:
        raise ValueError("PHASE4AP_SNAPSHOT_SQLITE_CORRUPT") from exc
    if catalog["integrity_check"] != "ok":
        raise ValueError("PHASE4AP_SNAPSHOT_INTEGRITY_FAILURE")
    return catalog


def build(
    source_db: Path,
    snapshot_path: Path,
    restore_path: Path,
    production_db: Path,
    *,
    research_db: Path | None,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AP_EVALUATION_TIMEZONE_MISSING")
    if snapshot_path.exists() or restore_path.exists():
        raise FileExistsError("PHASE4AP_OUTPUT_DATABASE_EXISTS")
    path_inputs: dict[str, tuple[Path, bool]] = {
        "source": (source_db, True),
        "snapshot": (snapshot_path, False),
        "restore": (restore_path, False),
        "production": (production_db, True),
    }
    if research_db is not None:
        path_inputs["research"] = (research_db, True)
    path_proof = _assert_distinct(path_inputs)
    production_before = _file_identity(production_db)
    research_before = None if research_db is None else _file_identity(research_db)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    restore_path.parent.mkdir(parents=True, exist_ok=True)
    source = _open_ro(source_db)
    _assert_marker(source)
    source_catalog = _catalog(source)
    snapshot = sqlite3.connect(snapshot_path)
    try:
        source.backup(snapshot)
        snapshot.commit()
    finally:
        snapshot.close()
        source.close()
    with snapshot_path.open("rb+") as handle:
        os.fsync(handle.fileno())
    snapshot_file_hash = _file_hash(snapshot_path)
    snapshot_catalog = verify_snapshot(snapshot_path, snapshot_file_hash)
    snapshot_ro = _open_ro(snapshot_path)
    restore = sqlite3.connect(restore_path)
    try:
        snapshot_ro.backup(restore)
        restore.commit()
    finally:
        restore.close()
        snapshot_ro.close()
    with restore_path.open("rb+") as handle:
        os.fsync(handle.fileno())
    restore_ro = _open_ro(restore_path)
    _assert_marker(restore_ro)
    restore_catalog = _catalog(restore_ro)
    restore_ro.close()
    equality = {
        "schema_equal": source_catalog["schema_hash"] == restore_catalog["schema_hash"],
        "row_counts_equal": source_catalog["row_counts_hash"] == restore_catalog["row_counts_hash"],
        "logical_hash_equal": source_catalog["logical_hash"] == restore_catalog["logical_hash"],
        "snapshot_logical_hash_equal": source_catalog["logical_hash"]
        == snapshot_catalog["logical_hash"],
        "restore_integrity_ok": restore_catalog["integrity_check"] == "ok",
    }
    if not all(equality.values()):
        raise ValueError("PHASE4AP_RESTORE_VERIFICATION_FAILED")
    production_after = _file_identity(production_db)
    research_after = None if research_db is None else _file_identity(research_db)
    if production_before != production_after:
        raise ValueError("PHASE4AP_PRODUCTION_METADATA_CHANGED")
    if research_before != research_after:
        raise ValueError("PHASE4AP_RESEARCH_METADATA_CHANGED")
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AP",
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "path_isolation": path_proof,
        "source_file_hash": _file_hash(source_db),
        "source_catalog": source_catalog,
        "snapshot_file_hash": snapshot_file_hash,
        "snapshot_catalog_hash": canonical_hash(snapshot_catalog),
        "snapshot_identity_verified": True,
        "production_metadata_unchanged": True,
        "research_metadata_unchanged": research_db is None or research_before == research_after,
        "production_database_mutated": False,
        "research_database_mutated": False,
        "database_mutation_performed": False,
        "disposable_backup_created": True,
        "disposable_restore_created": True,
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    pair_id = canonical_hash(
        {"snapshot": snapshot_file_hash, "source": source_catalog["logical_hash"], "now": now}
    )
    report["publication_pair_id"] = pair_id
    report["artifact_hash"] = _hash(report)
    proof: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "phase": "4AP",
        "publication_pair_id": pair_id,
        "backup_verification_hash": report["artifact_hash"],
        "restore_file_hash": _file_hash(restore_path),
        "restore_catalog": restore_catalog,
        "equality_proof": equality,
        "all_equalities_verified": all(equality.values()),
        "restore_target_distinct": True,
        "production_or_research_restore_performed": False,
        "production_execution_authorized": False,
    }
    proof["manifest_hash"] = _hash(proof, "manifest_hash")
    return report, proof


def _file_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--snapshot-db", type=Path, required=True)
    parser.add_argument("--restore-db", type=Path, required=True)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--research-db", type=Path)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4AP_EVALUATION_TIME_INVALID") from exc
    if now.tzinfo is None:
        raise ValueError("PHASE4AP_EVALUATION_TIMEZONE_MISSING")
    report, proof = build(
        args.source_db,
        args.snapshot_db,
        args.restore_db,
        args.production_db,
        research_db=args.research_db,
        now=now,
    )
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.report_output, args.proof_output, report, proof)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
