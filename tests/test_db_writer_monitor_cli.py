from __future__ import annotations

import json

from typer.testing import CliRunner

from kalshi_predictor.cli import app


def test_db_writer_monitor_json_stdout_is_valid_json(monkeypatch) -> None:
    payload = {
        "status": "OPEN_READERS",
        "safe_to_start_write": True,
        "current_writer_pid": None,
        "recommended_next_action": "line one\nline two",
    }
    monkeypatch.setattr("kalshi_predictor.cli.db_writer_monitor", lambda settings=None: payload)

    result = CliRunner().invoke(app, ["db-writer-monitor", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == payload
    assert "line one\\nline two" in result.output
