from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.system_readiness.remediation import (
    run_system_readiness_remediation,
    system_remediation_card,
)
from kalshi_predictor.ui.app import create_app


def test_system_remediation_is_paper_only_and_writes_report(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_path = Path(tmp_path) / "remediation.md"

    result = run_system_readiness_remediation(
        settings=_settings(tmp_path),
        output_path=output_path,
        refresh_data=False,
        session_factory=session_factory,
    )

    assert result["live_trading_authorized"] is False
    assert result["demo_trading_authorized"] is False
    assert result["safety"]["paper_only_confirmed"] is True
    assert result["safety"]["demo_execution_attempted"] is False
    assert result["safety"]["live_execution_attempted"] is False
    assert output_path.exists()
    assert "This remediation does not submit live orders." in output_path.read_text(
        encoding="utf-8"
    )


def test_system_remediation_blocks_malformed_sqlite_without_session_use(tmp_path) -> None:
    bad_db = Path(tmp_path) / "bad.db"
    bad_db.write_text("not a sqlite database", encoding="utf-8")
    output_path = Path(tmp_path) / "blocked.md"

    result = run_system_readiness_remediation(
        settings=_settings(tmp_path, db_url=f"sqlite:///{bad_db}"),
        output_path=output_path,
        refresh_data=True,
    )

    assert result["status"] == "BLOCKED"
    assert any(step["name"] == "database-recovery-required" for step in result["steps"])
    assert "Move SQLite out of OneDrive" in output_path.read_text(encoding="utf-8")
    assert result["safety"]["order_write_attempted"] is False


def test_system_remediation_blocks_live_environment(tmp_path) -> None:
    output_path = Path(tmp_path) / "live_blocked.md"

    result = run_system_readiness_remediation(
        settings=_settings(tmp_path, kalshi_env="production"),
        output_path=output_path,
        refresh_data=True,
    )

    assert result["status"] == "BLOCKED"
    assert any(step["name"] == "paper-only-safety" for step in result["steps"])
    assert result["live_trading_authorized"] is False


def test_system_remediation_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    output_path = Path(tmp_path) / "cli_remediation.md"
    db_url = f"sqlite:///{Path(tmp_path) / 'cli.db'}"

    result = runner.invoke(
        app,
        [
            "system-remediation-report",
            "--output",
            str(output_path),
        ],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "Live trading authorized: false" in result.output
    assert output_path.exists()


def test_system_remediation_card_and_system_page_render(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = _settings(tmp_path)
    with session_factory() as session:
        card = system_remediation_card(session, settings=settings)

    assert card["paper_only_confirmed"] is True
    assert "kalshi-bot system-remediate --refresh-data" in card["next_commands"]

    client = TestClient(create_app(session_factory=session_factory, settings=settings))
    response = client.get("/system")

    assert response.status_code == 200
    assert "System Remediation" in response.text
    assert "Paper-only confirmed" in response.text
    assert "kalshi-bot system-remediate --refresh-data" in response.text


def _settings(
    tmp_path,
    *,
    db_url: str | None = None,
    kalshi_env: str = "demo",
) -> Settings:
    return Settings(
        kalshi_env=kalshi_env,
        kalshi_db_url=db_url or f"sqlite:///{Path(tmp_path) / 'remediation.db'}",
        execution_enabled=False,
        execution_dry_run=True,
        ui_read_only=True,
        phase_3w_system_certification_enabled=True,
        phase_3w_mode="AUDIT_ONLY",
        phase_3x_professional_ux_enabled=True,
        phase_3x_mode="preview",
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'remediation.db'}")
    return get_session_factory(engine)
