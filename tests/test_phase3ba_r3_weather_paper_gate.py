from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_r3
from kalshi_predictor.cli import app


def _base_row() -> dict:
    return {
        "current_window_eligible": True,
        "verified_kalshi_url": True,
        "source_identity_ready": True,
        "has_snapshot": True,
        "snapshot_fresh": True,
        "has_weather_source_forecast": True,
        "weather_source_forecast_fresh": True,
        "has_weather_feature": True,
        "weather_feature_fresh": True,
        "has_current_forecast": True,
        "has_current_ranking": True,
        "raw_ev": "0.05",
        "executable_ev": "0.03",
        "no_book_reason": None,
        "executable_book": True,
        "settlement_terms_known": True,
        "paper_entry_settlement_eligible": True,
        "phase3s_proceed": True,
        "phase3m_nonzero_size": True,
        "phase3n_approved": True,
    }


def test_phase3ba_r3_first_weather_paper_blocker_order() -> None:
    cases = [
        ({"source_identity_ready": False}, "MARKET_SOURCE_MISSING"),
        ({"verified_kalshi_url": False}, "MARKET_LINK_UNVERIFIED"),
        ({"has_snapshot": False}, "SNAPSHOT_MISSING"),
        ({"snapshot_fresh": False}, "SNAPSHOT_STALE"),
        ({"has_weather_source_forecast": False}, "WEATHER_SOURCE_MISSING"),
        ({"weather_source_forecast_fresh": False}, "WEATHER_SOURCE_STALE"),
        ({"has_weather_feature": False}, "WEATHER_FEATURE_MISSING"),
        ({"weather_feature_fresh": False}, "WEATHER_FEATURE_STALE"),
        ({"has_current_forecast": False}, "FORECAST_MISSING"),
        ({"has_current_ranking": False}, "RANKING_MISSING"),
        ({"raw_ev": "0"}, "EV_NOT_POSITIVE"),
        ({"executable_ev": "0"}, "EXECUTABLE_EV_NOT_POSITIVE"),
        ({"executable_book": False, "no_book_reason": "INSUFFICIENT_DEPTH"}, "LIQUIDITY_TOO_LOW"),
        ({"executable_book": False, "no_book_reason": "WIDE_SPREAD"}, "SPREAD_TOO_WIDE"),
        ({"executable_book": False, "no_book_reason": "NO_ORDERBOOK_SNAPSHOT"}, "BOOK_MISSING"),
        ({"settlement_terms_known": False}, "SETTLEMENT_TERMS_UNKNOWN"),
        ({"phase3s_proceed": False}, "RISK_NOT_ELIGIBLE"),
        ({"phase3m_nonzero_size": False}, "PHASE_3M_ZERO_SIZE"),
        ({"phase3n_approved": False}, "PHASE_3N_RISK_BLOCK"),
        ({}, "PAPER_READY"),
    ]
    for patch, expected in cases:
        row = _base_row()
        row.update(patch)
        assert phase3ba_r3._first_weather_paper_blocker(row) == expected


def test_phase3ba_r3_reports_every_failed_gate() -> None:
    row = _base_row()
    row.update(
        {
            "verified_kalshi_url": False,
            "snapshot_fresh": False,
            "raw_ev": "0",
            "executable_ev": "0",
            "phase3n_approved": False,
        }
    )

    blockers = phase3ba_r3._weather_paper_blockers(row)

    assert blockers == [
        "MARKET_LINK_UNVERIFIED",
        "SNAPSHOT_STALE",
        "EV_NOT_POSITIVE",
        "EXECUTABLE_EV_NOT_POSITIVE",
        "PHASE_3N_RISK_BLOCK",
    ]


def test_phase3ba_r3_accepts_exact_catalog_api_identity_without_promoting_ui_url() -> None:
    identity = {
        "market_ticker": "KXTEMPNYCH-TEST",
        "api_url": (
            "https://external-api.kalshi.com/trade-api/v2/markets/"
            "KXTEMPNYCH-TEST"
        ),
        "kalshi_url_status": "BUILT_FROM_EXACT_CATALOG",
        "kalshi_url_verified": False,
    }

    assert phase3ba_r3._weather_source_identity_ready(
        identity,
        ticker="KXTEMPNYCH-TEST",
    ) is True
    assert identity["kalshi_url_verified"] is False


def test_phase3ba_r3_summary_counts_ready_and_blockers() -> None:
    ready = _base_row()
    ready["first_blocker"] = "PAPER_READY"
    blocked = _base_row()
    blocked.update({"snapshot_fresh": False, "first_blocker": "SNAPSHOT_STALE"})

    summary = phase3ba_r3._summary([ready, blocked])

    assert summary["current_weather_links"] == 2
    assert summary["paper_ready_rows"] == 1
    assert summary["first_hard_blocker"] == "SNAPSHOT_STALE"
    assert summary["first_hard_blocker_counts"] == {
        "PAPER_READY": 1,
        "SNAPSHOT_STALE": 1,
    }


def test_phase3ba_r3_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-r3-weather-paper-gate", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-r3-weather-paper-gate" in result.output
