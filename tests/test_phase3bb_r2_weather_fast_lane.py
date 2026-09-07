from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3bb_r2
from kalshi_predictor.cli import app


def test_weather_candidates_merge_ranking_and_gate_rows() -> None:
    candidates = phase3bb_r2.weather_candidates_from_reports(
        r2_payload={
            "weather_rows": [
                {
                    "ticker": "KXTEMPNYCH-TEST",
                    "location_key": "new_york",
                    "target_time": "2026-07-11T10:00:00+00:00",
                    "has_current_ranking": True,
                    "estimated_edge": "0.03",
                    "opportunity_score": "67",
                    "ranking_reason": "Positive weather edge.",
                }
            ]
        },
        r3_payload={
            "weather_rows": [
                {
                    "ticker": "KXTEMPNYCH-TEST",
                    "location_key": "new_york",
                    "market_title": "NYC high temperature",
                    "current_window_eligible": True,
                    "verified_kalshi_url": True,
                    "has_weather_source_forecast": True,
                    "weather_source_forecast_fresh": True,
                    "has_weather_feature": True,
                    "weather_feature_fresh": True,
                    "snapshot_fresh": True,
                    "has_current_forecast": True,
                    "has_current_ranking": True,
                    "raw_ev": "0.03",
                    "executable_ev": "-0.01",
                    "executable_book": False,
                    "settlement_terms_known": True,
                    "phase3s_proceed": False,
                    "phase3m_nonzero_size": False,
                    "phase3n_approved": False,
                    "paper_ready": False,
                    "first_blocker": "EXECUTABLE_EV_NOT_POSITIVE",
                }
            ]
        },
    )

    assert candidates[0]["ticker"] == "KXTEMPNYCH-TEST"
    assert candidates[0]["weather_ranking"] is True
    assert candidates[0]["positive_ev"] is True
    assert candidates[0]["positive_executable_ev"] is False
    assert candidates[0]["first_blocker"] == "EXECUTABLE_EV_NOT_POSITIVE"


def test_weather_fast_lane_summary_counts_exact_blockers() -> None:
    rows = [
        {
            "verified_kalshi_link": True,
            "weather_source_evidence": True,
            "weather_source_fresh": True,
            "weather_snapshot_fresh": True,
            "weather_feature": True,
            "weather_forecast": True,
            "weather_ranking": True,
            "positive_ev": True,
            "positive_executable_ev": False,
            "executable_book": False,
            "risk_gate_eligible": False,
            "paper_ready": False,
            "first_blocker": "EXECUTABLE_EV_NOT_POSITIVE",
        },
        {
            "verified_kalshi_link": True,
            "weather_source_evidence": False,
            "weather_source_fresh": False,
            "weather_snapshot_fresh": True,
            "weather_feature": False,
            "weather_forecast": False,
            "weather_ranking": False,
            "positive_ev": False,
            "positive_executable_ev": False,
            "executable_book": False,
            "risk_gate_eligible": False,
            "paper_ready": False,
            "first_blocker": "SOURCE_MISSING",
        },
    ]

    summary = phase3bb_r2.weather_fast_lane_summary(
        rows,
        writer={"safe_to_start_write": True, "current_writer_pid": None},
        r2_payload={"status": "WEATHER_RANKING_GATE_CLOSED"},
        r3_payload={"status": "WEATHER_PAPER_GATE_BLOCKED"},
        r5_payload={"category_summaries": {"weather": {"status": "weather_v2"}}},
    )

    assert summary["current_weather_rows"] == 2
    assert summary["ranking_rows"] == 1
    assert summary["positive_ev_rows"] == 1
    assert summary["first_hard_blocker"] == "SOURCE_MISSING"
    assert summary["first_hard_blocker_counts"]["EXECUTABLE_EV_NOT_POSITIVE"] == 1


def test_weather_fast_lane_acceptance_allows_writer_blocked_no_orders() -> None:
    acceptance = phase3bb_r2._acceptance(
        summary={
            "ranking_rows": 0,
            "first_hard_blocker_counts": {},
            "dashboard_weather_status": "weather_v2",
            "dashboard_weather_current_rows": 0,
        },
        writer_active=True,
    )

    assert acceptance["weather_rows_ranked_or_exact_blocker"] is True
    assert acceptance["no_live_demo_or_paper_orders"] is True
    assert acceptance["no_paper_trades_created"] is True


def test_no_current_weather_rows_is_exact_blocker_with_refresh_next_action() -> None:
    summary = {
        "ranking_rows": 0,
        "first_hard_blocker": "NO_CURRENT_WEATHER_ROWS",
        "first_hard_blocker_counts": {},
        "dashboard_weather_status": "phase3az_r13_weather_handoff",
        "dashboard_weather_current_rows": 20,
        "paper_ready_rows": 0,
    }

    acceptance = phase3bb_r2._acceptance(summary=summary, writer_active=False)
    next_action = phase3bb_r2._next_action(summary=summary, writer_active=False)

    assert acceptance["weather_rows_ranked_or_exact_blocker"] is True
    assert next_action["stage"] == "REFRESH_ACTIVE_WEATHER_CATALOG"
    assert "sync-markets --status open" in next_action["command"]
    assert next_action["allow_paper_trade_creation"] is False


def test_phase3bb_r2_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r2-weather-fast-lane", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r2-weather-fast-lane" in result.output
