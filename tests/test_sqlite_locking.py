import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import kalshi_predictor.cli as cli_module
from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import init_db
from kalshi_predictor.data.locks import (
    db_writer_monitor,
    friendly_database_locked_message,
    is_database_locked_error,
    sqlite_lock_diagnostics,
)
from kalshi_predictor.ui.app import create_app


def test_db_init_sets_wal_and_busy_timeout(tmp_path) -> None:
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'locking.db'}")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar()

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 30000
    assert synchronous == 1


def test_ui_route_handles_locked_database_operational_error(monkeypatch, tmp_path) -> None:
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'busy.db'}")
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    def locked_dashboard(self):
        del self
        raise OperationalError(
            "SELECT 1",
            {},
            sqlite3.OperationalError("database is locked"),
        )

    monkeypatch.setattr(
        "kalshi_predictor.ui.routes.DecisionUiService.dashboard",
        locked_dashboard,
    )
    client = TestClient(
        create_app(
            session_factory=factory,
            settings=Settings(overnight_require_market_data=False),
        )
    )

    response = client.get("/")

    assert response.status_code == 503
    assert "Database is busy. Try refreshing in a few seconds." in response.text


def test_ui_route_closes_session_after_query(tmp_path) -> None:
    closed_sessions = []

    class TrackingSession(Session):
        def close(self) -> None:
            closed_sessions.append(self)
            super().close()

    engine = init_db(f"sqlite:///{Path(tmp_path) / 'close.db'}")
    factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=TrackingSession,
    )
    client = TestClient(
        create_app(
            session_factory=factory,
            settings=Settings(overnight_require_market_data=False),
        )
    )

    response = client.get("/opportunities")

    assert response.status_code == 200
    assert closed_sessions


def test_sqlite_lock_diagnostics_reports_database_identity(tmp_path) -> None:
    db_path = Path(tmp_path) / "locks.db"
    db_url = f"sqlite:///{db_path}"
    init_db(db_url)

    payload = sqlite_lock_diagnostics(db_url=db_url)

    assert payload["backend"] == "SQLite"
    assert payload["database_path"] == str(db_path.resolve())
    assert str(db_path.resolve()) in payload["target_files"]
    assert payload["status"] in {"CLEAR", "OPEN_READERS", "BUSY_WRITER", "UNKNOWN"}


def test_database_locked_helper_detects_sqlalchemy_wrapped_lock(tmp_path) -> None:
    db_url = f"sqlite:///{Path(tmp_path) / 'friendly.db'}"
    exc = OperationalError(
        "SELECT 1",
        {},
        sqlite3.OperationalError("database is locked"),
    )

    message = friendly_database_locked_message(db_url=db_url)

    assert is_database_locked_error(exc)
    assert "Database is busy" in message
    assert "Next action:" in message


def test_db_writer_monitor_reports_active_writer() -> None:
    payload = db_writer_monitor(
        diagnostics={
            "backend": "SQLite",
            "database_url": "sqlite:///data/kalshi_phase1.db",
            "database_path": "/tmp/kalshi_phase1.db",
            "scan_method": "procfs",
            "safe_to_write": False,
            "status": "BUSY_WRITER",
            "next_action": "Wait for the listed writer job to finish.",
            "holders": [
                {
                    "pid": 1234,
                    "command": "/venv/bin/kalshi-bot link-remediate",
                    "open_files": ["/tmp/kalshi_phase1.db"],
                    "current_process": False,
                    "likely_writer": True,
                    "elapsed_seconds": 3723,
                    "elapsed": "1h 02m 03s",
                }
            ],
            "writer_holders": [
                {
                    "pid": 1234,
                    "command": "/venv/bin/kalshi-bot link-remediate",
                    "open_files": ["/tmp/kalshi_phase1.db"],
                    "current_process": False,
                    "likely_writer": True,
                    "elapsed_seconds": 3723,
                    "elapsed": "1h 02m 03s",
                }
            ],
        }
    )

    assert payload["status"] == "WRITER_ACTIVE"
    assert payload["current_writer_pid"] == 1234
    assert payload["current_writer_elapsed"] == "1h 02m 03s"
    assert payload["safe_to_start_write"] is False
    assert payload["recommended_next_command_after_finish"] == (
        "kalshi-bot derive-sports-schedule --build-features"
    )


def test_db_writer_monitor_marks_stale_old_heartbeat_as_orphan(monkeypatch) -> None:
    monkeypatch.setattr(
        "kalshi_predictor.data.locks.load_latest_long_job_status",
        lambda: {
            "status": "STALE",
            "heartbeat_age": "17h",
            "heartbeat": {
                "pid": 999999,
                "stage": "CRYPTO_LINK_START",
                "processed": 0,
                "total": None,
            },
        },
    )
    monkeypatch.setattr("kalshi_predictor.data.locks._pid_exists", lambda pid: False)

    payload = db_writer_monitor(
        diagnostics={
            "backend": "SQLite",
            "database_url": "sqlite:///data/kalshi_phase1.db",
            "database_path": "/tmp/kalshi_phase1.db",
            "scan_method": "procfs",
            "safe_to_write": False,
            "status": "BUSY_WRITER",
            "next_action": "Wait for the listed writer job to finish.",
            "holders": [
                {
                    "pid": 1234,
                    "command": "/venv/bin/kalshi-bot phase3bc-r5-crypto-freshness-watch",
                    "open_files": ["/tmp/kalshi_phase1.db"],
                    "current_process": False,
                    "likely_writer": True,
                    "elapsed_seconds": 3723,
                    "elapsed": "1h 02m 03s",
                }
            ],
            "writer_holders": [
                {
                    "pid": 1234,
                    "command": "/venv/bin/kalshi-bot phase3bc-r5-crypto-freshness-watch",
                    "open_files": ["/tmp/kalshi_phase1.db"],
                    "current_process": False,
                    "likely_writer": True,
                    "elapsed_seconds": 3723,
                    "elapsed": "1h 02m 03s",
                }
            ],
        }
    )

    assert payload["status"] == "WRITER_ACTIVE"
    assert payload["long_job_heartbeat_status"] == "STALE"
    assert payload["long_job_heartbeat_display_status"] == "STALE_ORPHANED"
    assert payload["long_job_heartbeat_matches_current_writer"] is False
    assert payload["long_job_heartbeat_stale_orphaned"] is True


def test_db_locks_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    db_url = f"sqlite:///{Path(tmp_path) / 'locks_cli.db'}"
    init_db(db_url)

    result = CliRunner().invoke(app, ["db-locks"], env={"KALSHI_DB_URL": db_url})
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert "Database lock diagnostics" in result.output
    assert "Safe to start another write job" in result.output


def test_db_writer_monitor_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    db_url = f"sqlite:///{Path(tmp_path) / 'writer_monitor.db'}"
    init_db(db_url)

    result = CliRunner().invoke(app, ["db-writer-monitor"], env={"KALSHI_DB_URL": db_url})
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert "DB writer monitor" in result.output
    assert "Current writer PID" in result.output
    assert "Recommended next command after finish" in result.output


def test_runtime_identity_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    db_url = f"sqlite:///{Path(tmp_path) / 'runtime.db'}"
    init_db(db_url)

    result = CliRunner().invoke(app, ["runtime-identity"], env={"KALSHI_DB_URL": db_url})
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert "Runtime identity" in result.output
    assert "Python executable" in result.output
    assert "Package path" in result.output


def test_cli_database_locked_errors_are_friendly(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    db_url = f"sqlite:///{Path(tmp_path) / 'locked_cli.db'}"

    def raise_locked(*args, **kwargs):
        del args, kwargs
        raise OperationalError(
            "SELECT 1",
            {},
            sqlite3.OperationalError("database is locked"),
        )

    monkeypatch.setattr(cli_module, "database_health", raise_locked)

    result = CliRunner().invoke(app, ["db-health"], env={"KALSHI_DB_URL": db_url})
    get_settings.cache_clear()

    assert result.exit_code == 75
    assert "Database is busy" in result.output
    assert "Wait for settlement/learning jobs to finish" in result.output
