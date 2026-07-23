from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_r3
from kalshi_predictor.cli import app


def _base_row() -> dict:
    return {
        "current_window_eligible": True,
        "verified_kalshi_url": True,
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
        ({"verified_kalshi_url": False}, "SOURCE_MISSING"),
        ({"snapshot_fresh": False}, "SNAPSHOT_STALE"),
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


def test_weather_gate_reuses_location_evidence_across_tickers() -> None:
    target = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
    links = [
        SimpleNamespace(ticker="KXTEMPNYCH-A", location_key="new_york", target_time=target),
        SimpleNamespace(ticker="KXTEMPNYCH-B", location_key="new_york", target_time=target),
    ]

    class FakeSession:
        def __init__(self, candidates):
            self.candidates = candidates
            self.calls = 0

        def scalars(self, statement):
            self.calls += 1
            return iter(self.candidates)

    feature = SimpleNamespace(target_time=target)
    feature_session = FakeSession([feature])
    features = phase3ba_r3._weather_features_for_links(
        feature_session,
        links,
        settings=SimpleNamespace(weather_v2_default_location_key="new_york"),
        match_tolerance_hours=3,
    )
    assert feature_session.calls == 1
    assert features == {link.ticker: feature for link in links}

    source = SimpleNamespace(forecast_time=target)
    source_session = FakeSession([source])
    forecasts = phase3ba_r3._weather_source_forecasts_for_links(
        source_session,
        links,
        match_tolerance_hours=3,
    )
    assert source_session.calls == 1
    assert forecasts == {link.ticker: source for link in links}


def test_phase3ba_r3_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-r3-weather-paper-gate", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-r3-weather-paper-gate" in result.output
