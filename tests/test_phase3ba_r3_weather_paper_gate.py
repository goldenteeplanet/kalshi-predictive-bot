from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_r3
from kalshi_predictor.cli import app


def _base_row() -> dict:
    return {
        "current_window_eligible": True,
        "terminal_weather_horizon_complete": True,
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
        ({"current_window_eligible": False}, "MARKET_WINDOW_INELIGIBLE"),
        ({"terminal_weather_horizon_complete": False}, "RAIN_HORIZON_INCOMPLETE"),
        ({"verified_kalshi_url": False}, "LINK_UNVERIFIED"),
        ({"has_snapshot": False}, "SNAPSHOT_MISSING"),
        ({"snapshot_fresh": False}, "SNAPSHOT_STALE"),
        ({"has_current_forecast": False}, "FORECAST_MISSING"),
        ({"has_current_ranking": False}, "RANKING_MISSING"),
        ({"raw_ev": "0"}, "EV_NOT_POSITIVE"),
        ({"executable_ev": "0"}, "EXECUTABLE_EV_NOT_POSITIVE"),
        ({"executable_book": False, "no_book_reason": "INSUFFICIENT_DEPTH"}, "LIQUIDITY_TOO_LOW"),
        (
            {"executable_book": False, "no_book_reason": "INSUFFICIENT_BUY_SIDE_SIZE"},
            "LIQUIDITY_TOO_LOW",
        ),
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
    assert "--deadline-seconds" in result.output
    assert "--batch-size" in result.output


def test_weather_gate_rejects_unknown_location_links() -> None:
    links = [
        SimpleNamespace(ticker="UNKNOWN", location_key="unknown"),
        SimpleNamespace(ticker="NYC", location_key="new_york"),
    ]

    assert phase3ba_r3._valid_weather_links(links) == [links[1]]


def test_weather_gate_progress_is_atomic_and_marks_partial(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"

    phase3ba_r3._write_gate_progress(
        path,
        generated_at="2026-08-20T12:00:00+00:00",
        rows=[{"ticker": "ONE"}],
        total=2,
        deadline_reached=True,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "PARTIAL_DEADLINE_REACHED"
    assert payload["rows_processed"] == 1
    assert payload["complete"] is False
    assert not path.with_suffix(".json.tmp").exists()


def test_weather_book_probe_accepts_exact_identity_and_audits_actual_buy_level() -> None:
    now = datetime.now(UTC)
    ranking = SimpleNamespace(
        best_side="BUY_YES",
        best_price="0.26",
        liquidity_score="60",
        time_to_close_minutes="120",
    )
    market = SimpleNamespace(status="active", close_time=now + timedelta(hours=2))
    snapshot = SimpleNamespace(
        raw_orderbook_json=json.dumps(
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.18", "100"]],
                    "no_dollars": [["0.74", "3.78"]],
                }
            }
        )
    )
    settings = SimpleNamespace(
        opportunity_max_spread=Decimal("0.10"),
        opportunity_min_time_to_close_minutes=Decimal("10"),
    )

    book = phase3ba_r3._book_probe(
        ranking=ranking,
        market=market,
        identity={"exact_market_identity_verified": True},
        snapshot=snapshot,
        snapshot_age=Decimal("1"),
        settings=settings,
        window={"current_window_eligible": True},
    )

    assert book["executable_book"] is True
    assert book["derived_executable_buy_price"] == "0.26"
    assert book["ranked_buy_price"] == "0.26"
    assert book["buy_price_matches_ranking"] is True
    assert book["actual_buy_price_source"] == "NO_BID_COMPLEMENT"
    assert book["best_yes_bid_depth"] == "100"
    assert book["best_no_bid_depth"] == "3.78"
    assert book["depth_at_configured_limit"] == "3.78"
    assert book["sufficient_buy_side_size"] is True


def test_weather_book_probe_rejects_stale_ranked_price_before_sizing() -> None:
    now = datetime.now(UTC)
    book = phase3ba_r3._book_probe(
        ranking=SimpleNamespace(
            best_side="BUY_YES",
            best_price="0.25",
            liquidity_score="60",
            time_to_close_minutes="120",
        ),
        market=SimpleNamespace(status="active", close_time=now + timedelta(hours=2)),
        identity={"exact_market_identity_verified": True},
        snapshot=SimpleNamespace(
            raw_orderbook_json=json.dumps(
                {
                    "orderbook_fp": {
                        "yes_dollars": [["0.18", "100"]],
                        "no_dollars": [["0.74", "3.78"]],
                    }
                }
            )
        ),
        snapshot_age=Decimal("1"),
        settings=SimpleNamespace(
            opportunity_max_spread=Decimal("0.10"),
            opportunity_min_time_to_close_minutes=Decimal("10"),
        ),
        window={"current_window_eligible": True},
    )

    assert book["executable_book"] is False
    assert book["no_book_reason"] == "BUY_PRICE_MISMATCH"
    assert book["buy_price_matches_ranking"] is False
