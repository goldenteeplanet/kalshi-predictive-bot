from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import typer
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from kalshi_predictor.cli import app, link_coverage_command
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import init_db, make_sqlite_read_only_engine


def test_link_coverage_cli_help_exposes_database_read_only_mode() -> None:
    result = CliRunner().invoke(app, ["link-coverage", "--help"])

    assert result.exit_code == 0
    assert "--database-read-only" in result.output
    assert "mode=ro plus PRAGMA query_only=ON" in result.output


def test_sqlite_read_only_engine_rejects_writes(tmp_path: Path) -> None:
    database_path = tmp_path / "coverage.db"
    engine = init_db(f"sqlite:///{database_path}")
    engine.dispose()
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()

    read_only_engine = make_sqlite_read_only_engine(f"sqlite:///{database_path}")
    with read_only_engine.connect() as connection:
        assert connection.execute(text("PRAGMA query_only")).scalar_one() == 1
        with pytest.raises(DBAPIError):
            connection.execute(text("CREATE TABLE forbidden_write (id INTEGER)"))
    read_only_engine.dispose()

    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before


def test_link_coverage_read_only_cli_writes_only_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "coverage.db"
    engine = init_db(f"sqlite:///{database_path}")
    engine.dispose()
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    get_settings.cache_clear()

    result = CliRunner().invoke(
        app,
        [
            "link-coverage",
            "--database-read-only",
            "--output",
            str(tmp_path / "link_coverage_report.md"),
        ],
    )
    get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert "SQLite mode=ro + PRAGMA query_only=ON" in result.output
    assert "Database writes: 0" in result.output
    assert (tmp_path / "link_coverage_report.md").is_file()
    snapshot = json.loads(
        (tmp_path / "reports/market_coverage/link_coverage.json").read_text(encoding="utf-8")
    )
    sports = next(row for row in snapshot["category_rows"] if row["category"] == "sports")
    assert sports["current_coverage_display"] == "n/a"
    assert sports["current_linked_markets"] == 0
    assert sports["current_linkable_markets"] == 0
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    ("parse_first", "parse_limit", "refresh"),
    [(True, 0, False), (False, 1, False), (False, 0, True)],
)
def test_link_coverage_read_only_rejects_parse_options(
    parse_first: bool,
    parse_limit: int,
    refresh: bool,
) -> None:
    with pytest.raises(
        typer.BadParameter,
        match="cannot be combined with parsing or refresh options",
    ):
        link_coverage_command(
            output=Path("unused.md"),
            parse_first=parse_first,
            parse_limit=parse_limit,
            refresh=refresh,
            database_read_only=True,
        )


def test_link_coverage_snapshot_systemd_contract() -> None:
    service = Path("deploy/systemd/kalshi-link-coverage-snapshot.service").read_text(
        encoding="utf-8"
    )
    timer = Path("deploy/systemd/kalshi-link-coverage-snapshot.timer").read_text(
        encoding="utf-8"
    )

    assert "User=kalshi" in service
    assert "--database-read-only" in service
    assert "--parse-first" not in service
    assert "EXECUTION_ENABLED=false" in service
    assert "AUTOPILOT_ENABLED=false" in service
    assert "PAPER_ORDER_CREATION_ENABLED=false" in service
    assert "PAPER_ORDER_KILL_SWITCH=true" in service
    assert "EXECUTION_KILL_SWITCH=true" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=/opt/kalshi-predictive-bot/reports" in service
    assert "OnUnitActiveSec=15min" in timer
    assert "Persistent=false" in timer
