import json

import typer
from typer.testing import CliRunner

from kalshi_predictor.overnight_paper.cli import register_commands


def test_empty_registry_cli_records_exact_blocker_and_preserves_archive(tmp_path):
    app = typer.Typer()
    register_commands(app)
    root = tmp_path / "scan"
    runner = CliRunner()
    result = runner.invoke(app, ["qualified-candidate-scan", "--archive-root", str(root)])
    assert result.exit_code == 0, result.output
    stored = (root / "qualified_scan.json").read_bytes()
    report = json.loads(stored)
    assert report["network_requests"] == 0
    assert "NO_CERTIFIED_FAMILY" in result.output
    assert (
        runner.invoke(app, ["qualified-candidate-scan", "--archive-root", str(root)]).exit_code != 0
    )
    assert (root / "qualified_scan.json").read_bytes() == stored
