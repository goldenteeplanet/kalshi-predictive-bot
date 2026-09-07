from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.ui.progress import (
    _PROGRESS_CACHE,
    MAX_PROGRESS_SNAPSHOT_BYTES,
    build_progress_dashboard,
)
from kalshi_predictor.ui.progress_history import MAX_HISTORY_BYTES, load_progress_timeline


def _snapshot(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-20T00:00:00Z",
                "execution_enabled": False,
                "paper_enabled": False,
                "collector": {"poll_interval_seconds": 30},
                "writer": {"state": "PASSED", "safe_to_start_write": True},
            }
        ),
        encoding="utf-8",
    )


def test_progress_cache_has_one_entry_and_bounded_refresh_cadence() -> None:
    assert _PROGRESS_CACHE.max_entries == 1
    assert _PROGRESS_CACHE.ttl_seconds == 30


def test_oversized_snapshot_fails_closed_without_materialization(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KALSHI_CERTIFICATION_REPORTS_ROOT", str(tmp_path))
    path = tmp_path / "oversized.json"
    path.write_bytes(b"{" + b" " * MAX_PROGRESS_SNAPSHOT_BYTES + b"}")
    result = build_progress_dashboard(path)
    assert "STATUS_SNAPSHOT_TOO_LARGE" in result["diagnostics"]
    assert result["active_process"]["state"] == "BLOCKED"
    assert result["execution"]["enabled"] is False


def test_oversized_history_is_not_loaded(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    path.write_bytes(b"{" + b" " * MAX_HISTORY_BYTES + b"}")
    result = load_progress_timeline(path)
    assert result["entries"] == []
    assert result["count"] == 0


def test_repeated_local_dashboard_build_has_bounded_python_peak(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KALSHI_CERTIFICATION_REPORTS_ROOT", str(tmp_path))
    path = tmp_path / "snapshot.json"
    _snapshot(path)
    tracemalloc.start()
    for _ in range(12):
        result = build_progress_dashboard(path)
        assert result["read_only"] is True
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 16 * 1024 * 1024


def test_ui_command_exposes_worker_recycle_and_keepalive_guards() -> None:
    result = CliRunner().invoke(app, ["ui", "--help"])
    assert result.exit_code == 0
    assert "--limit-max-requests" in result.output
    assert "--timeout-keep-alive" in result.output
