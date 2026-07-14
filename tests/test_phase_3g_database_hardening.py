from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from kalshi_predictor.cli import (
    PHASE_3G_EXPECTED_COMMANDS,
    _alembic_config,
    app,
    build_command_audit,
)
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data import maintenance
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    detect_backend,
    is_postgres,
    is_sqlite,
    redact_database_url,
    warn_if_sqlite_on_onedrive,
)
from kalshi_predictor.data.db import get_session_factory, init_db, make_engine
from kalshi_predictor.data.maintenance import (
    BLOCKED,
    READY,
    database_doctor,
    generate_database_report,
    migrate_sqlite_to_postgres,
)
from kalshi_predictor.tonight.control import build_tonight_check
from kalshi_predictor.ui.app import create_app


def test_backend_detection_and_database_url_from_settings() -> None:
    sqlite_settings = Settings(db_backend="sqlite", kalshi_db_url="sqlite:///data/test.db")
    postgres_settings = Settings(
        db_backend="postgres",
        kalshi_db_url="sqlite:///data/test.db",
        postgres_user="bot",
        postgres_password="secret",
        postgres_host="db",
        postgres_port=5433,
        postgres_db="kalshi_test",
    )

    assert detect_backend(sqlite_settings) == "sqlite"
    assert is_sqlite(sqlite_settings)
    assert is_postgres(postgres_settings)
    assert database_url_from_settings(postgres_settings).startswith("postgresql+psycopg://")
    assert "secret" in database_url_from_settings(postgres_settings)


def test_sqlite_on_onedrive_warning() -> None:
    warning = warn_if_sqlite_on_onedrive(
        db_url="sqlite:///C:/Users/user1/OneDrive/kalshi/phase1.db"
    )

    assert warning is not None
    assert "OneDrive" in warning


def test_sqlite_engine_sets_timeout_and_pragmas(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'engine.db'}")

    with engine.connect() as connection:
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar()
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()

    assert busy_timeout == 30000
    assert synchronous == 1
    assert str(journal_mode).lower() == "wal"


def test_db_health_command_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    db_url = f"sqlite:///{tmp_path / 'health.db'}"
    result = CliRunner().invoke(app, ["db-health"], env={"DATABASE_URL": db_url})
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert "Database health" in result.output
    assert "READY" in result.output


def test_db_doctor_reports_missing_sqlite_database(tmp_path) -> None:
    payload = database_doctor(db_url=f"sqlite:///{tmp_path / 'missing.db'}")

    assert payload["status"] == BLOCKED
    assert "does not exist" in payload["items"][0]["message"]


def test_migration_tool_handles_empty_tables(tmp_path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    init_db(f"sqlite:///{source}")

    result = migrate_sqlite_to_postgres(
        sqlite_url=f"sqlite:///{source}",
        postgres_url=f"sqlite:///{target}",
    )

    assert result["status"] == READY
    assert result["rows_copied"] == 0
    assert target.exists()
    assert any(table["table"] == "markets" for table in result["tables"])


def test_database_report_generation(tmp_path) -> None:
    output = tmp_path / "database_report.md"
    path = generate_database_report(
        output_path=output,
        db_url=f"sqlite:///{tmp_path / 'report.db'}",
    )

    text = path.read_text(encoding="utf-8")
    assert "Database Health Report" in text
    assert "Status: READY" in text


def test_database_settings_page_and_dashboard_card_render(tmp_path) -> None:
    engine = init_db(f"sqlite:///{tmp_path / 'ui.db'}")
    session_factory = get_session_factory(engine)
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    settings_response = client.get("/settings/database")
    dashboard_response = client.get("/")

    assert settings_response.status_code == 200
    assert "Database Settings" in settings_response.text
    assert "Operator Commands" in settings_response.text
    assert dashboard_response.status_code == 200
    assert "Database" in dashboard_response.text


def test_database_status_card_uses_fast_health_check(monkeypatch, tmp_path) -> None:
    engine = init_db(f"sqlite:///{tmp_path / 'ui-fast-health.db'}")
    session_factory = get_session_factory(engine)
    seen: dict[str, bool] = {}

    def fake_check_connection(executor, items, summary, *, include_integrity=True):
        del executor
        seen["include_integrity"] = include_integrity
        items.append(
            {
                "name": "DB reachable",
                "status": READY,
                "message": "Database connection succeeded.",
            }
        )
        summary["dialect"] = "sqlite"
        summary["sqlite"] = {
            "busy_timeout": 30000,
            "journal_mode": "wal",
            "synchronous": 1,
        }

    monkeypatch.setattr(maintenance, "_check_connection", fake_check_connection)

    with session_factory() as session:
        card = maintenance.database_status_card(session, settings=Settings())

    assert seen["include_integrity"] is False
    assert card["status"] == READY


def test_tonight_check_blocks_malformed_sqlite(monkeypatch, tmp_path) -> None:
    engine = init_db(f"sqlite:///{tmp_path / 'malformed.db'}")
    session_factory = get_session_factory(engine)

    def broken_integrity_check(session):
        del session
        raise SQLAlchemyError("database disk image is malformed")

    monkeypatch.setattr(
        "kalshi_predictor.tonight.control._sqlite_integrity_check",
        broken_integrity_check,
    )
    with session_factory() as session:
        check = build_tonight_check(
            session,
            settings=Settings(),
            project_path=tmp_path,
            reports_dir=tmp_path / "reports",
            check_port=False,
        )

    assert check.status == BLOCKED
    assert check.recovery_instructions


def test_postgres_url_redacts_password() -> None:
    redacted = redact_database_url("postgresql+psycopg://user:secret@localhost:5432/db")

    assert "secret" not in redacted
    assert "***" in redacted


def test_database_cli_help_includes_hardening_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "db-health" in result.output
    assert "db-doctor" in result.output
    assert "db-migrate" in result.output
    assert "sqlite-backup" in result.output


def test_alembic_config_resolves_from_installed_repo_when_cwd_differs(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = _alembic_config(f"sqlite:///{tmp_path / 'migration.db'}")
    script_location = Path(config.get_main_option("script_location"))

    assert script_location.is_absolute()
    assert script_location.name == "alembic"
    assert (script_location / "env.py").exists()


def test_phase_3g_expected_commands_are_registered() -> None:
    audit = build_command_audit()

    assert audit["missing_commands"] == []
    for command in PHASE_3G_EXPECTED_COMMANDS:
        assert command in audit["registered_commands"]


def test_phase_3g_command_help_smoke() -> None:
    runner = CliRunner()
    for command in PHASE_3G_EXPECTED_COMMANDS:
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_phase_status_command_smoke() -> None:
    result = CliRunner().invoke(app, ["phase-status"])

    assert result.exit_code == 0
    assert "Phase 3A installed" in result.output
    assert "Phase 3G installed" in result.output
    assert "Phase 3O installed" in result.output
    assert "missing modules" in result.output


def test_command_audit_command_smoke() -> None:
    result = CliRunner().invoke(app, ["command-audit"])

    assert result.exit_code == 0
    assert "Expected commands:" in result.output
    assert "db-migrate: registered" in result.output
    assert "Missing commands: none" in result.output
