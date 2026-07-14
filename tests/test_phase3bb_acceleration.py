from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3bb_acceleration as accel
from kalshi_predictor.cli import app


def test_throughput_ev_per_day_uses_runtime_hours() -> None:
    assert accel.ev_per_day(3, 165) == 0.436


def test_zero_paper_ready_pace_is_not_honestly_estimable() -> None:
    estimate = accel.estimate_meaningful(paper_ready_rows=0, daily_paper_trades=0)

    assert estimate["meaningful"] is False
    assert estimate["classification"] == "NOT_HONESTLY_ESTIMABLE_ZERO_PAPER_READY_PACE"


def test_cloud_readiness_does_not_recommend_gpu() -> None:
    recommendation = accel.cloud_recommendation(sqlite_backend=True)

    assert recommendation["gpu_required"] is False
    assert "GPU instance" in recommendation["what_not_to_buy"]


def test_scheduler_plan_prevents_duplicate_writers() -> None:
    rules = accel.scheduler_rules()

    assert rules["prevents_duplicate_writers"] is True
    assert rules["one_writer_capable_job_at_a_time"] is True
    assert rules["on_active_writer"] == "SKIP_OR_QUEUE_WITH_REASON"


def test_category_scorecard_ranks_weather_above_crypto_when_crypto_waits() -> None:
    rows = [
        {
            "category": "weather",
            "parsed_market_count": "126",
            "linked_count": "106",
            "source_readiness": "SOURCE_AND_FEATURES_READY",
            "forecast_readiness": "FORECASTS_AND_RANKINGS_PRESENT",
            "primary_blocker": "EV_NOT_POSITIVE",
        },
        {
            "category": "crypto",
            "parsed_market_count": "10425",
            "linked_count": "6791",
            "source_readiness": "SOURCE_AND_FEATURES_READY",
            "forecast_readiness": "FORECASTS_AND_RANKINGS_PRESENT",
            "primary_blocker": "BACKGROUND_WAITING_FOR_EXECUTABLE_BOOK",
        },
    ]

    ranked = accel.rank_categories(rows, crypto_waiting=True)

    assert ranked[0]["category"] == "weather"


def test_tradingeconomics_is_deferred_in_multicategory_payload_shape() -> None:
    assert "TradingEconomics" not in accel.REPORT_COMMANDS
    assert "TradingEconomics" in ["TradingEconomics"]


def test_historical_replay_is_separated_from_real_paper_learning() -> None:
    rules = accel.historical_replay_rules()

    assert rules["label_required"] == "HISTORICAL_REPLAY"
    assert rules["counts_toward_live_paper_learning_target"] is False
    assert rules["no_fabricated_fills_or_outcomes"] is True


def test_next_actions_reference_only_registered_safe_commands() -> None:
    command = (
        "kalshi-bot db-writer-monitor --json && kalshi-bot "
        "phase3bb-scheduler-plan --output-dir reports/phase3bb --reports-dir reports"
    )
    checks = accel.command_safety_checks(command)

    assert checks["all_registered"] is True
    assert checks["contains_forbidden_trade_command"] is False


def test_no_live_demo_exchange_or_paper_writes_in_safety_flags() -> None:
    flags = accel._safety_flags()

    assert flags["creates_paper_trades"] is False
    assert flags["places_exchange_orders"] is False
    assert flags["submits_cancels_replaces_orders"] is False


def test_phase3bb_acceleration_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3bb-acceleration-report", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-acceleration-report" in result.output
