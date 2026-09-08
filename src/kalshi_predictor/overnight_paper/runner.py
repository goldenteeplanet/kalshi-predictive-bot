"""Bounded observation cycles with a single SQLite evidence writer and durable resume.

This module never imports activation, shadow creation, or exchange execution.
Each cycle starts a fresh public discovery window. Crash-orphan archives are kept.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.overnight_paper.discovery import run_discovery

TABLE = "overnight_sprint_cycles"


def run_observation_cycles(
    database: Path,
    archive_root: Path,
    *,
    run_id: str,
    cycles: int = 3,
    interval_seconds: int = 60,
    max_pages: int = 40,
    max_book_requests: int = 24,
    cycle_timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Run/resume a total target count; recorded errors stop until separately reviewed.

    BEGIN IMMEDIATE stays held during acquisition, so another database writer
    cannot overlap a cycle. Only an INSERT into the evidence table is authorized.
    Committing releases the lock between cycles; the next acquisition rechecks
    committed checkpoints before doing work. No stale lock/PID inference is used.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise ValueError("INVALID_RUN_ID")
    if not 1 <= cycles <= 24 or not 60 <= interval_seconds <= 3600:
        raise ValueError("INVALID_CYCLE_LIMITS")
    if not 1 <= max_pages <= 100 or not 1 <= max_book_requests <= 100:
        raise ValueError("INVALID_DISCOVERY_LIMITS")
    if not 1 <= cycle_timeout_seconds <= 600:
        raise ValueError("INVALID_CYCLE_TIMEOUT")
    database = _path(database, require_file=True)
    archive_root = _path(archive_root, require_file=False)
    if archive_root == database or database.is_relative_to(archive_root):
        raise ValueError("DATABASE_MUST_BE_SEPARATE_FROM_ARCHIVE")
    archive_root.mkdir(parents=True, exist_ok=True)
    config = {
        "database": str(database),
        "archive_root": str(archive_root),
        "interval_seconds": interval_seconds,
        "max_pages": max_pages,
        "max_book_requests": max_book_requests,
        "cycle_timeout_seconds": cycle_timeout_seconds,
        "mode": "OBSERVATION_ONLY",
        "discovery_window": "FRESH_EACH_CYCLE",
    }
    completed: list[dict[str, Any]] = []
    # mode=rw never creates a database or migrates an unrelated application DB.
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=0)) as db:
        _validate_database(db)
        db.set_authorizer(_authorize)
        while True:
            db.execute("BEGIN IMMEDIATE")
            try:
                completed = _checkpoints(db, run_id, config)
                if len(completed) >= cycles:
                    db.rollback()
                    break
                if completed and completed[-1]["status"] == "ERROR":
                    raise ValueError("PREVIOUS_CYCLE_ERROR_REQUIRES_REVIEW")
                if completed:
                    previous = datetime.fromisoformat(completed[-1]["captured_at"])
                    remaining = interval_seconds - (datetime.now(UTC) - previous).total_seconds()
                    if remaining > 0:
                        db.rollback()
                        time.sleep(min(remaining, 60))
                        continue
                index = len(completed) + 1
                cycle_root = archive_root / f"{run_id}-{index:03d}-{uuid.uuid4().hex}"
                record: dict[str, Any] = {
                    "run_id": run_id,
                    "cycle": index,
                    "config": config,
                    "captured_at": datetime.now(UTC).isoformat(),
                    "status": "EVIDENCE_CAPTURED",
                    "mode": "OBSERVATION_ONLY",
                    "orders_created": 0,
                    "shadows_created": 0,
                    "archive_root": str(cycle_root),
                }
                try:
                    record["result"] = run_discovery(
                        cycle_root,
                        max_pages=max_pages,
                        max_book_requests=max_book_requests,
                        timeout_seconds=cycle_timeout_seconds,
                        resume_from=None,
                    )
                except Exception as exc:
                    record.update(status="ERROR", error=f"{type(exc).__name__}: {exc}")
                record["finished_at"] = datetime.now(UTC).isoformat()
                # Preserve discovery/provider clocks verbatim; only the wrapper has new clocks.
                normalized = json.loads(json.dumps(record, default=str, allow_nan=False))
                normalized["sha256"] = _digest(normalized)
                db.execute(
                    f"INSERT INTO {TABLE}(id,captured_at,payload) VALUES(?,?,?)",
                    (f"{run_id}:{index:03d}", record["captured_at"], _encode(normalized)),
                )
                db.commit()
                completed.append(normalized)
                if record["status"] == "ERROR":
                    break
            except BaseException:
                db.rollback()
                raise
    return {
        "mode": "OBSERVATION_ONLY",
        "run_id": run_id,
        "status": "ERROR" if completed and completed[-1]["status"] == "ERROR" else "COMPLETE",
        "cycles_completed": len(completed),
        "target_cycles": cycles,
        "database": str(database),
        "archive_root": str(archive_root),
        "orders_created": 0,
        "shadows_created": 0,
        "checkpoints": [f"{run_id}:{row['cycle']:03d}" for row in completed],
    }


def _path(path: Path, *, require_file: bool) -> Path:
    absolute = path.absolute()
    if "onedrive" in str(absolute).lower():
        raise ValueError("UNSYNCED_PATH_REQUIRED")
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)()
        for p in (absolute, *absolute.parents)
    ):
        raise ValueError("LINKED_PATH_REFUSED")
    if require_file and not absolute.is_file():
        raise ValueError("EXISTING_SPRINT_DATABASE_REQUIRED")
    return absolute.resolve()


def _validate_database(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise ValueError("DATABASE_INTEGRITY_FAILED")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {
        TABLE,
        "overnight_shadow",
        "overnight_history",
        "weather_settlement_rules",
        "paper_orders",
        "paper_fills",
        "paper_positions",
    }
    if not required <= tables:
        raise ValueError("ISOLATED_SPRINT_SCHEMA_REQUIRED")
    if [row[1] for row in db.execute(f"PRAGMA table_info({TABLE})")] != [
        "id",
        "captured_at",
        "payload",
    ]:
        raise ValueError("CYCLE_SCHEMA_MISMATCH")
    if db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchone():
        raise ValueError("DATABASE_TRIGGERS_REFUSED")


def _authorize(
    action: int, table: str | None, column: str | None, database: str | None, trigger: str | None
) -> int:
    if action in {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
    }:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_INSERT and table == TABLE and database == "main" and not trigger:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _checkpoints(
    db: sqlite3.Connection, run_id: str, config: dict[str, Any]
) -> list[dict[str, Any]]:
    prefix = run_id + ":"
    rows = db.execute(
        f"SELECT id,captured_at,payload FROM {TABLE} WHERE substr(id,1,?)=? ORDER BY id",
        (len(prefix), prefix),
    ).fetchall()
    results = []
    for index, (key, captured, raw) in enumerate(rows, 1):
        payload = json.loads(raw)
        expected = payload.pop("sha256", None)
        if expected != _digest(payload):
            raise ValueError("CHECKPOINT_HASH_MISMATCH")
        if key != f"{run_id}:{index:03d}" or payload.get("cycle") != index:
            raise ValueError("CHECKPOINT_SEQUENCE_MISMATCH")
        if payload.get("config") != config or payload.get("run_id") != run_id:
            raise ValueError("CHECKPOINT_CONFIG_MISMATCH")
        timestamp = datetime.fromisoformat(captured)
        if timestamp.tzinfo is None or timestamp > datetime.now(UTC):
            raise ValueError("INVALID_CHECKPOINT_TIME")
        if captured != payload.get("captured_at"):
            raise ValueError("CHECKPOINT_TIME_MISMATCH")
        if payload.get("status") not in {"EVIDENCE_CAPTURED", "ERROR"}:
            raise ValueError("INVALID_CHECKPOINT_STATUS")
        payload["sha256"] = expected
        results.append(payload)
    return results


def _encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_encode(payload).encode()).hexdigest()
