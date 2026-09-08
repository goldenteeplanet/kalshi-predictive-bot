"""Standalone settlement command cooperates with the supervisor OS owner."""

import test_overnight_activation as fixtures
import typer
from sqlalchemy import text
from typer.testing import CliRunner

from kalshi_predictor.overnight_paper.cli import register_commands
from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner

baseline_template = fixtures.baseline_template
prepared = fixtures.prepared


def test_standalone_monitor_cannot_compete_with_active_owner(prepared):
    app = typer.Typer()
    register_commands(app)
    with acquire_runtime_owner(prepared["database_path"]):
        result = CliRunner().invoke(app, [
            "paper-settlement-cycles", "--database", str(prepared["database_path"]),
        ])
    assert result.exit_code != 0
    assert "RUNTIME_OWNER_ALREADY_ACTIVE" in str(result.exception)


def test_standalone_empty_cycle_releases_owner_without_claiming_active(prepared):
    with prepared["session_factory"]() as session:
        session.execute(text("DELETE FROM overnight_shadow"))
        session.commit()
    app = typer.Typer()
    register_commands(app)
    result = CliRunner().invoke(app, [
        "paper-settlement-cycles", "--database", str(prepared["database_path"]),
    ])
    assert result.exit_code == 0, result.output
    assert "NO_TRACKED_MARKETS" in result.output
    with acquire_runtime_owner(prepared["database_path"]):
        pass
