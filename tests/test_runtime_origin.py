from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.runtime_origin import (
    DATABASE_PATH_MISMATCH,
    EDITABLE_INSTALL_MISMATCH,
    ONEDRIVE_REFERENCE,
    READY,
    build_runtime_origin,
)


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    values = {
        "KALSHI_DB_URL": f"sqlite:///{tmp_path / 'data' / 'kalshi.db'}",
        "KALSHI_DEVELOPMENT_ROOT": str(tmp_path / "development"),
        "KALSHI_RUNTIME_ROOT": str(tmp_path / "runtime"),
        "KALSHI_REPORT_ROOT": str(tmp_path / "runtime" / "reports"),
        "KALSHI_UI_REPORT_ROOT": str(tmp_path / "published" / "reports"),
        "KALSHI_RESEARCH_DATA_ROOT": str(tmp_path / "research"),
        "KALSHI_WRITER_LOCK": str(tmp_path / "locks" / "writer.lock"),
        "KALSHI_STAGING_ROOT": str(tmp_path / "staging"),
    }
    values.update(overrides)
    return Settings(**values)


def test_runtime_origin_ready_for_intentional_split_layout(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    development = Path(settings.kalshi_development_root or "")
    runtime = Path(settings.kalshi_runtime_root or "")
    package = runtime / "src" / "kalshi_predictor" / "__init__.py"
    development.mkdir(parents=True)
    runtime.mkdir(parents=True)
    package.parent.mkdir(parents=True)
    package.write_text("", encoding="utf-8")

    payload = build_runtime_origin(
        settings=settings,
        cwd=runtime,
        package_file=package,
        python_executable=runtime / ".venv" / "bin" / "python",
    )

    assert payload["status"] == READY
    assert payload["layout"]["intentional_separation"] is True
    assert payload["layout"]["runtime_import_matches"] is True
    assert payload["layout"]["database_outside_checkouts"] is True


def test_runtime_origin_reports_install_database_and_onedrive_mismatches(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    settings = _settings(
        tmp_path,
        KALSHI_DB_URL=f"sqlite:///{runtime / 'data' / 'kalshi.db'}",
        KALSHI_RESEARCH_DATA_ROOT=str(tmp_path / "OneDrive" / "research"),
    )
    runtime.mkdir(parents=True)
    outside_package = tmp_path / "other" / "kalshi_predictor" / "__init__.py"
    outside_package.parent.mkdir(parents=True)
    outside_package.write_text("", encoding="utf-8")

    payload = build_runtime_origin(
        settings=settings,
        cwd=runtime,
        package_file=outside_package,
    )

    assert EDITABLE_INSTALL_MISMATCH in payload["statuses"]
    assert DATABASE_PATH_MISMATCH in payload["statuses"]
    assert ONEDRIVE_REFERENCE in payload["statuses"]


def test_runtime_origin_cli_writes_reports(tmp_path: Path) -> None:
    get_settings.cache_clear()
    json_path = tmp_path / "runtime_origin.json"
    markdown_path = tmp_path / "runtime_origin.md"
    result = CliRunner().invoke(
        app,
        [
            "runtime-origin",
            "--json-output",
            str(json_path),
            "--markdown-output",
            str(markdown_path),
        ],
        env={
            "KALSHI_DB_URL": f"sqlite:///{tmp_path / 'kalshi.db'}",
            "KALSHI_DEVELOPMENT_ROOT": str(Path.cwd()),
            "KALSHI_RUNTIME_ROOT": str(Path.cwd()),
        },
    )
    get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert "Runtime origin" in result.output
    assert json_path.is_file()
    assert markdown_path.is_file()
