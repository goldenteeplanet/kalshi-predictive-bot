from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_r2
from kalshi_predictor.cli import app


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        opportunity_min_edge=phase3ba_r2.Decimal("0.01"),
        opportunity_min_score=phase3ba_r2.Decimal("60"),
        opportunity_max_spread=phase3ba_r2.Decimal("0.20"),
        opportunity_min_liquidity=phase3ba_r2.Decimal("1"),
        opportunity_min_time_to_close_minutes=phase3ba_r2.Decimal("5"),
    )


def _metadata() -> dict:
    return {
        "generated_at": "2026-07-10T21:00:00+00:00",
        "repository_root": "/tmp/repo",
        "git_branch": "main",
        "git_commit": "abc123",
        "git_dirty": "clean",
        "python_executable": "/tmp/python",
        "installed_package_path": "/tmp/phase3ba_r2.py",
        "resolved_database_url": "sqlite:///tmp/test.db",
        "database_fingerprint": {"kind": "sqlite_file_stat", "fingerprint": "db123"},
        "database_location": "/tmp/test.db",
        "migration_revision": "rev1",
        "timezone": "UTC",
        "command_arguments": {
            "command": "kalshi-bot phase3ba-r2-weather-ranking-activation",
            "argv": ["phase3ba-r2-weather-ranking-activation"],
        },
        "data_watermark": {"latest_weather_v2_forecast_at": "2026-07-10T20:59:00+00:00"},
        "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "safety_flags": phase3ba_r2._safety_flags(),
    }


def test_phase3ba_r2_blocks_when_writer_active(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ba_r2, "_metadata", lambda *args, **kwargs: _metadata())
    monkeypatch.setattr(
        phase3ba_r2,
        "_monitor_writer",
        lambda _settings: {
            "status": "WRITER_ACTIVE",
            "safe_to_start_write": False,
            "current_writer_pid": 19983,
        },
    )
    monkeypatch.setattr(
        phase3ba_r2,
        "_current_weather_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("weather rows should not be inspected after writer block")
        ),
    )

    artifacts = phase3ba_r2.write_phase3ba_r2_weather_ranking_activation_report(
        object(),
        output_dir=Path("reports/phase3ba_r2"),
        reports_dir=Path("reports"),
        settings=object(),
        command_args=["phase3ba-r2-weather-ranking-activation"],
    )
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["status"] == "BLOCKED_BY_ACTIVE_WRITER"
    assert payload["opportunity_scan"]["ran"] is False
    assert payload["next_action"]["command"] == "kalshi-bot db-writer-monitor --json"


def test_phase3ba_r2_first_weather_blockers() -> None:
    settings = _settings()

    assert (
        phase3ba_r2._first_weather_blocker(
            {"has_snapshot": False, "has_current_forecast": False}, settings=settings
        )
        == "SNAPSHOT_MISSING"
    )
    assert (
        phase3ba_r2._first_weather_blocker(
            {"has_snapshot": True, "has_current_forecast": False}, settings=settings
        )
        == "FORECAST_MISSING"
    )
    assert (
        phase3ba_r2._first_weather_blocker(
            {
                "has_snapshot": True,
                "has_current_forecast": True,
                "has_current_ranking": False,
            },
            settings=settings,
        )
        == "RANKING_MISSING"
    )
    assert (
        phase3ba_r2._first_weather_blocker(
            {
                "has_snapshot": True,
                "has_current_forecast": True,
                "has_current_ranking": True,
                "best_side": "YES",
                "best_price": "0.40",
                "estimated_edge": "-0.01",
            },
            settings=settings,
        )
        == "EV_NOT_POSITIVE"
    )
    assert (
        phase3ba_r2._first_weather_blocker(
            {
                "has_snapshot": True,
                "has_current_forecast": True,
                "has_current_ranking": True,
                "best_side": "YES",
                "best_price": "0.40",
                "estimated_edge": "0.05",
                "liquidity": "0",
            },
            settings=settings,
        )
        == "LIQUIDITY_TOO_LOW"
    )
    assert (
        phase3ba_r2._first_weather_blocker(
            {
                "has_snapshot": True,
                "has_current_forecast": True,
                "has_current_ranking": True,
                "best_side": "YES",
                "best_price": "0.40",
                "estimated_edge": "0.05",
                "liquidity": "5",
                "spread": "0.01",
                "settlement_terms_known": True,
                "opportunity_score": "70",
                "time_to_close_minutes": "30",
            },
            settings=settings,
        )
        == "PAPER_GATE_READY"
    )


def test_phase3ba_r2_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-r2-weather-ranking-activation", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-r2-weather-ranking-activation" in result.output


def test_current_weather_links_applies_exact_ticker_scope() -> None:
    statements = []

    class FakeSession:
        def scalars(self, statement):
            statements.append(statement)
            return []

    rows = phase3ba_r2._current_weather_links(
        FakeSession(),
        current_since=phase3ba_r2.utc_now(),
        limit=6,
        tickers=["KXTEMPNYCH-A", "KXTEMPNYCH-A", "KXTEMPCHI-B"],
    )

    assert rows == []
    assert len(statements) == 1
    sql = str(statements[0])
    assert "weather_market_links.ticker IN" in sql
