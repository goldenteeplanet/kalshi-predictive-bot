from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    backend_label,
    database_url_from_settings,
    detect_backend,
    redact_database_url,
    sqlite_path_from_url,
    warn_if_sqlite_on_onedrive,
)
from kalshi_predictor.data.db import describe_db_location, make_engine
from kalshi_predictor.data.schema import Base
from kalshi_predictor.system_certification.migration_diagnostics import (
    ALEMBIC_AT_HEAD,
    ALEMBIC_UPGRADE_REQUIRED,
    alembic_graph_diagnostics,
    latest_head_revision,
)

READY = "READY"
WARNING = "WARNING"
BLOCKED = "BLOCKED"
_REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_HEAD = latest_head_revision(root=_REPO_ROOT)
RECOVERY_INSTRUCTIONS = (
    "Stop other bot/UI processes, close DB viewers, move SQLite out of OneDrive, "
    "restore a known-good backup if integrity_check fails, then rerun db-doctor."
)


def database_health(
    session: Session | None = None,
    *,
    settings: Settings | None = None,
    db_url: str | None = None,
    include_integrity: bool = True,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    url = _url_from_session(session) or db_url or database_url_from_settings(resolved_settings)
    items: list[dict[str, str]] = []
    summary: dict[str, Any] = {
        "backend": detect_backend(resolved_settings, db_url=url),
        "backend_label": backend_label(resolved_settings, db_url=url),
        "database_url": redact_database_url(url),
        "location": describe_db_location(url),
    }

    try:
        if session is not None:
            _check_connection(session, items, summary, include_integrity=include_integrity)
        else:
            engine = make_engine(url)
            with engine.connect() as connection:
                _check_connection(connection, items, summary, include_integrity=include_integrity)
    except (OSError, SQLAlchemyError, sqlite3.DatabaseError) as exc:
        items.append(_item("DB health", BLOCKED, str(exc) or type(exc).__name__))
        return _payload(items, summary, recovery=RECOVERY_INSTRUCTIONS)

    warning = warn_if_sqlite_on_onedrive(resolved_settings, db_url=url)
    summary["sqlite_on_onedrive_warning"] = warning
    if warning:
        items.append(_item("SQLite OneDrive safety", WARNING, warning))
    else:
        items.append(_item("SQLite OneDrive safety", READY, "No SQLite OneDrive warning."))

    migration = migration_status(session=session, settings=resolved_settings, db_url=url)
    summary["migration"] = migration
    items.append(_item("Alembic migrations", migration["status"], migration["message"]))

    return _payload(items, summary)


def database_status_card(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    health = database_health(session, settings=settings, include_integrity=False)
    migration = health["summary"].get("migration") or {}
    return {
        "status": health["status"],
        "backend": health["summary"]["backend_label"],
        "location": health["summary"]["location"],
        "database_url": health["summary"]["database_url"],
        "migration_status": migration.get("status", "UNKNOWN"),
        "migration_message": migration.get("message", "Migration state unavailable."),
        "warning": health["summary"].get("sqlite_on_onedrive_warning"),
        "items": health["items"],
    }


def database_doctor(
    *,
    settings: Settings | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    url = db_url or database_url_from_settings(resolved_settings)
    items: list[dict[str, str]] = []
    summary = {
        "backend": detect_backend(resolved_settings, db_url=url),
        "backend_label": backend_label(resolved_settings, db_url=url),
        "database_url": redact_database_url(url),
        "location": describe_db_location(url),
    }

    path = sqlite_path_from_url(url)
    if path and str(path) != ":memory:" and not path.exists():
        items.append(
            _item(
                "SQLite file",
                BLOCKED,
                f"Database file does not exist: {path}",
            )
        )
        return _payload(items, summary, recovery="Run kalshi-bot init-db or restore a backup.")

    health = database_health(settings=resolved_settings, db_url=url)
    items.extend(health["items"])
    summary.update(health["summary"])

    if path and str(path) != ":memory:" and path.exists():
        try:
            with sqlite3.connect(path) as connection:
                result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            status = READY if result.lower() == "ok" else BLOCKED
            items.append(_item("SQLite raw integrity", status, result))
        except sqlite3.DatabaseError as exc:
            items.append(_item("SQLite raw integrity", BLOCKED, str(exc)))

    return _payload(items, summary, recovery=RECOVERY_INSTRUCTIONS)


def sqlite_backup(
    *,
    output_path: str | Path | None = None,
    settings: Settings | None = None,
    db_url: str | None = None,
) -> Path:
    url = db_url or database_url_from_settings(settings or get_settings())
    source = sqlite_path_from_url(url)
    if source is None or str(source) == ":memory:":
        raise ValueError("sqlite-backup requires a file-backed SQLite database.")
    if not source.exists():
        raise FileNotFoundError(source)

    if output_path is None:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
        output = Path("data/backups") / f"{source.stem}_{stamp}.db"
    else:
        output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(source) as src, sqlite3.connect(output) as dst:
        src.backup(dst)
    return output


def sqlite_recover(
    *,
    output_path: str | Path | None = None,
    settings: Settings | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    url = db_url or database_url_from_settings(settings or get_settings())
    source = sqlite_path_from_url(url)
    if source is None or str(source) == ":memory:":
        raise ValueError("sqlite-recover requires a file-backed SQLite database.")
    if not source.exists():
        return {
            "status": BLOCKED,
            "message": f"Database file does not exist: {source}",
            "recovery": "Restore a backup or run kalshi-bot init-db.",
        }

    backup_path = sqlite_backup(output_path=output_path, db_url=url)
    try:
        with sqlite3.connect(source) as connection:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        corrupt_copy = source.with_suffix(f"{source.suffix}.corrupt")
        shutil.copy2(source, corrupt_copy)
        return {
            "status": BLOCKED,
            "message": str(exc),
            "backup_path": str(backup_path),
            "corrupt_copy": str(corrupt_copy),
            "recovery": RECOVERY_INSTRUCTIONS,
        }

    return {
        "status": READY if result.lower() == "ok" else BLOCKED,
        "message": f"SQLite integrity_check returned {result}.",
        "backup_path": str(backup_path),
        "recovery": RECOVERY_INSTRUCTIONS if result.lower() != "ok" else "No recovery needed.",
    }


def migrate_sqlite_to_postgres(
    *,
    sqlite_url: str,
    postgres_url: str,
) -> dict[str, Any]:
    source_path = sqlite_path_from_url(sqlite_url)
    if source_path is not None and str(source_path) != ":memory:" and not source_path.exists():
        raise FileNotFoundError(source_path)

    source_engine = create_engine(sqlite_url, future=True)
    target_engine = make_engine(postgres_url)
    Base.metadata.create_all(target_engine)
    table_results: list[dict[str, Any]] = []
    rows_copied = 0

    with source_engine.connect() as source_connection, target_engine.begin() as target_connection:
        source_tables = set(inspect(source_connection).get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in source_tables:
                table_results.append({"table": table.name, "rows": 0, "status": "missing"})
                continue
            rows = [dict(row) for row in source_connection.execute(select(table)).mappings()]
            if rows:
                target_connection.execute(table.insert(), rows)
            rows_copied += len(rows)
            table_results.append({"table": table.name, "rows": len(rows), "status": "copied"})

    return {
        "status": READY,
        "source": describe_db_location(sqlite_url),
        "target": redact_database_url(postgres_url),
        "rows_copied": rows_copied,
        "tables": table_results,
    }


def migration_status(
    session: Session | None = None,
    *,
    settings: Settings | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    url = _url_from_session(session) or db_url or database_url_from_settings(resolved_settings)
    try:
        if session is not None:
            return _migration_status_for_connection(session)
        engine = make_engine(url)
        with engine.connect() as connection:
            return _migration_status_for_connection(connection)
    except SQLAlchemyError as exc:
        return {"status": WARNING, "message": f"Migration status unavailable: {exc}"}


def generate_database_report(
    *,
    output_path: str | Path = Path("reports/database_report.md"),
    settings: Settings | None = None,
    db_url: str | None = None,
) -> Path:
    health = database_health(settings=settings, db_url=db_url)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Database Health Report",
        "",
        f"- Generated at: {datetime.now(tz=UTC).isoformat()}",
        f"- Status: {health['status']}",
        f"- Backend: {health['summary'].get('backend_label')}",
        f"- Location: {health['summary'].get('location')}",
        f"- URL: {health['summary'].get('database_url')}",
        "",
        "## Checks",
        "",
    ]
    for item in health["items"]:
        lines.append(f"- {item['status']}: {item['name']} - {item['message']}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def _check_connection(
    executor: Any,
    items: list[dict[str, str]],
    summary: dict[str, Any],
    *,
    include_integrity: bool = True,
) -> None:
    executor.execute(text("SELECT 1")).scalar()
    items.append(_item("DB reachable", READY, "Database connection succeeded."))
    bind = executor.get_bind() if isinstance(executor, Session) else executor
    summary["dialect"] = bind.dialect.name

    if bind.dialect.name == "sqlite":
        if include_integrity:
            integrity = str(executor.execute(text("PRAGMA integrity_check")).scalar() or "")
            items.append(
                _item(
                    "SQLite integrity",
                    READY if integrity.lower() == "ok" else BLOCKED,
                    integrity,
                )
            )
        else:
            items.append(
                _item(
                    "SQLite quick status",
                    READY,
                    "Full integrity_check skipped for UI responsiveness; run db-health for audit.",
                )
            )
        busy_timeout = executor.execute(text("PRAGMA busy_timeout")).scalar()
        journal_mode = executor.execute(text("PRAGMA journal_mode")).scalar()
        synchronous = executor.execute(text("PRAGMA synchronous")).scalar()
        summary["sqlite"] = {
            "busy_timeout": busy_timeout,
            "journal_mode": str(journal_mode),
            "synchronous": synchronous,
        }
    else:
        items.append(_item("Isolation", READY, "PostgreSQL uses READ COMMITTED engine config."))


def _migration_status_for_connection(executor: Any) -> dict[str, Any]:
    bind = executor.get_bind() if isinstance(executor, Session) else executor
    inspector = inspect(bind)
    if not inspector.has_table("alembic_version"):
        diagnostics = alembic_graph_diagnostics([], root=_REPO_ROOT)
        return {
            "status": READY,
            "current_revision": None,
            "current_revisions": [],
            "head_revision": ",".join(diagnostics["head_revisions"]) or ALEMBIC_HEAD,
            "head_revisions": diagnostics["head_revisions"],
            "graph_status": diagnostics["status"],
            "script_location": diagnostics["script_location"],
            "message": "Alembic version table not initialized; legacy schema allowed.",
        }
    rows = executor.execute(text("SELECT version_num FROM alembic_version")).scalars()
    current_revisions = [str(row) for row in rows if row]
    diagnostics = alembic_graph_diagnostics(current_revisions, root=_REPO_ROOT)
    head_revision = ",".join(diagnostics["head_revisions"]) or ALEMBIC_HEAD
    if diagnostics["status"] == ALEMBIC_AT_HEAD:
        return {
            "status": READY,
            "current_revision": current_revisions[0] if current_revisions else None,
            "current_revisions": current_revisions,
            "head_revision": head_revision,
            "head_revisions": diagnostics["head_revisions"],
            "graph_status": diagnostics["status"],
            "script_location": diagnostics["script_location"],
            "message": diagnostics["message"],
        }
    status = WARNING if diagnostics["status"] == ALEMBIC_UPGRADE_REQUIRED else BLOCKED
    return {
        "status": status,
        "current_revision": current_revisions[0] if current_revisions else None,
        "current_revisions": current_revisions,
        "head_revision": head_revision,
        "head_revisions": diagnostics["head_revisions"],
        "graph_status": diagnostics["status"],
        "script_location": diagnostics["script_location"],
        "message": diagnostics["message"],
    }


def _url_from_session(session: Session | None) -> str | None:
    if session is None:
        return None
    bind = session.get_bind()
    return str(bind.url)


def _payload(
    items: list[dict[str, str]],
    summary: dict[str, Any],
    *,
    recovery: str = "",
) -> dict[str, Any]:
    severity = {READY: 0, WARNING: 1, BLOCKED: 2}
    status = max((item["status"] for item in items), key=lambda value: severity[value])
    return {"status": status, "items": items, "summary": summary, "recovery": recovery}


def _item(name: str, status: str, message: str) -> dict[str, str]:
    return {"name": name, "status": status, "message": message}
