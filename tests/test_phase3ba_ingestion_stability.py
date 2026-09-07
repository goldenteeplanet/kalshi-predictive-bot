from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_ingestion_stability as report
from kalshi_predictor.cli import app


def test_positive_ev_pace_projects_slow_conversion() -> None:
    pace = report._positive_ev_pace_summary(
        runtime_hours=165,
        observed_positive_ev=3,
    )

    assert pace["positive_ev_per_hour"] == 0.0182
    assert pace["positive_ev_per_day"] == 0.436
    assert pace["interpretation"] == "LOW_YIELD"
    assert pace["targets"][0]["target_positive_ev_rows"] == 10
    assert pace["targets"][0]["days_remaining_at_observed_pace"] == 16.0


def test_model_stability_blocks_when_gate_closed_and_target_unmet() -> None:
    assessment = report._model_stability_assessment(
        learning={
            "settled_paper_trades": 203,
            "target_settled_trades": 500,
            "daily_paper_trades": 0,
            "progress_percent": "40.6%",
            "expected_completion": "Needs today's paper trades before estimate",
        },
        status={"summary": {"paper_ready_rows": 0}},
        ev_pace={"positive_ev_per_day": 0.436},
    )

    assert assessment["status"] == "NOT_STABLE_AND_GATE_CLOSED"
    assert assessment["remaining_settled_trades"] == 297
    assert assessment["days_to_target_at_current_daily_paper_trade_pace"] is None
    assert assessment["proxy_days_to_target_if_each_observed_ev_became_a_settled_trade"] == 681.2


def test_ingestion_stability_svg_renders_labels() -> None:
    svg = report._bar_chart_svg(
        title="Example",
        subtitle="Test",
        rows=[{"label": "Forecasts", "value": 380289}, {"label": "Positive EV", "value": 3}],
        color="#123456",
    )

    assert svg.startswith("<svg")
    assert "Forecasts" in svg
    assert "Positive EV" in svg


def test_phase3ba_ingestion_stability_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-ingestion-stability-report", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-ingestion-stability-report" in result.output
