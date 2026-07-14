from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.workspace_guard import build_workspace_consistency_guard


def test_phase3bb_guard_blocks_missing_current_phase_command(tmp_path) -> None:
    repo_root = _fake_repo(tmp_path)
    payload = build_workspace_consistency_guard(
        settings=_settings(tmp_path),
        registered_commands=["db-writer-monitor", "phase-orchestrator"],
        cwd=repo_root,
        python_executable=repo_root / ".venv" / "bin" / "python",
        package_file=repo_root / "src" / "kalshi_predictor" / "__init__.py",
        env={"VIRTUAL_ENV": str(repo_root / ".venv")},
    )

    assert payload["summary"]["status"] == "BLOCKED"
    assert "phase3ah-sports-placeholder-watch" in payload["commands"][
        "missing_required_commands"
    ]
    assert any(item["code"] == "STALE_COMMAND_BUILD" for item in payload["findings"])
    assert payload["safety"]["exchange_writes"] is False


def test_phase3bb_guard_detects_wrong_terminal_directory(tmp_path) -> None:
    repo_root = _fake_repo(tmp_path)
    wrong_cwd = tmp_path / "other" / "kalshi-predictive-bot"
    wrong_cwd.mkdir(parents=True)

    payload = build_workspace_consistency_guard(
        settings=_settings(tmp_path),
        registered_commands=["db-writer-monitor", "phase3ah-sports-placeholder-watch"],
        cwd=wrong_cwd,
        python_executable=repo_root / ".venv" / "bin" / "python",
        package_file=repo_root / "src" / "kalshi_predictor" / "__init__.py",
        env={"VIRTUAL_ENV": str(repo_root / ".venv")},
    )

    assert payload["summary"]["status"] == "BLOCKED"
    assert any(item["code"] == "WORKSPACE_CWD_MISMATCH" for item in payload["findings"])


def test_phase3bb_guard_source_scan_handles_multiline_typer_decorators(tmp_path) -> None:
    repo_root = _fake_repo(tmp_path)
    (repo_root / "src" / "kalshi_predictor" / "cli.py").write_text(
        "\n".join(
            [
                'from typer import Typer',
                'app = Typer()',
                '@app.command("db-writer-monitor")',
                'def db_writer_monitor(): pass',
                '@app.command(',
                '    "runtime-identity",',
                '    help="Show runtime identity.",',
                ')',
                'def runtime_identity(): pass',
            ]
        ),
        encoding="utf-8",
    )

    payload = build_workspace_consistency_guard(
        settings=_settings(tmp_path),
        expected_commands=["db-writer-monitor", "runtime-identity"],
        cwd=repo_root,
        python_executable=repo_root / ".venv" / "bin" / "python",
        package_file=repo_root / "src" / "kalshi_predictor" / "__init__.py",
        env={"VIRTUAL_ENV": str(repo_root / ".venv")},
    )

    assert payload["commands"]["missing_required_commands"] == []
    assert payload["commands"]["command_source"] == "source_scan"


def test_phase3bb_guard_cli_help_and_report(tmp_path) -> None:
    output_dir = Path(tmp_path) / "phase3bb"
    runner = CliRunner()

    help_result = runner.invoke(app, ["phase3bb-workspace-guard", "--help"])
    result = runner.invoke(
        app,
        ["phase3bb-workspace-guard", "--output-dir", str(output_dir)],
        env={"DATABASE_URL": f"sqlite:///{Path(tmp_path) / 'guard_cli.db'}"},
    )

    assert help_result.exit_code == 0
    assert result.exit_code == 0
    assert output_dir.joinpath("phase3bb_workspace_guard.json").exists()
    assert "Phase 3BB Workspace / Build Guard" in result.output


def test_phase3bb_guard_renders_in_system_ui(tmp_path) -> None:
    client = TestClient(
        create_app(session_factory=_session_factory(tmp_path), settings=_settings(tmp_path))
    )

    page = client.get("/system")
    api = client.get("/api/workspace-guard")

    assert page.status_code == 200
    assert "Workspace / Build Guard" in page.text
    assert "DB fingerprint" in page.text
    assert "Build" in page.text
    assert api.status_code == 200
    assert api.json()["read_only"] is True
    assert "database_fingerprint" in api.json()["guard"]["database"]


def test_dashboard_current_snapshot_api_uses_bounded_status_snapshot(tmp_path) -> None:
    client = TestClient(
        create_app(session_factory=_session_factory(tmp_path), settings=_settings(tmp_path))
    )

    response = client.get("/api/dashboard/v1/snapshots/current")
    payload = response.json()

    assert response.status_code == 200
    assert payload["data"]["snapshot_mode"] == "BOUNDED_OPERATOR_STATUS"
    assert payload["dashboard_snapshot_id"].startswith("bounded-current-")
    assert payload["source_watermarks"][0]["database_fingerprint"] == payload["data"][
        "database_fingerprint"
    ]
    assert payload["source_watermarks"][0]["required"] is True
    assert payload["read_only_boundary"]["read_only"] is True


def _fake_repo(tmp_path) -> Path:
    repo_root = Path(tmp_path) / "kalshi-predictive-bot"
    package_dir = repo_root / "src" / "kalshi_predictor"
    package_dir.mkdir(parents=True)
    (repo_root / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "cli.py").write_text("", encoding="utf-8")
    (repo_root / ".venv" / "bin").mkdir(parents=True)
    return repo_root


def _settings(tmp_path) -> Settings:
    return Settings(
        kalshi_db_url=f"sqlite:///{Path(tmp_path) / 'phase3bb.db'}",
        phase_3x_professional_ux_enabled=True,
        phase_3x_mode="preview",
        phase_3w_system_certification_enabled=True,
        phase_3t_institutional_dashboard_enabled=True,
        phase_3t_mode="read_only_shadow",
        execution_enabled=False,
        execution_dry_run=True,
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3bb_ui.db'}")
    return get_session_factory(engine)
