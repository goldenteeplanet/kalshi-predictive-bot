from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import Forecast, Market, MarketRanking, MarketSnapshot
from kalshi_predictor.paper.models import BUY_YES
from kalshi_predictor.phase3aj_gap_closure import (
    build_composite_settlement_resolve,
    build_golden_trace_report,
    build_market_data_refresh_status,
    build_paper_trade_funnel,
    build_source_readiness_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3aj_paper_funnel_classifies_negative_raw_ev(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(session, probability="0.50", best_price="0.60")

        payload = build_paper_trade_funnel(session, settings=settings)

    assert payload["summary"]["rankings_reviewed"] == 1
    assert payload["reason_counts"]["NO_POSITIVE_RAW_EV"] == 1
    assert payload["summary"]["paper_orders_created"] == 0
    assert payload["summary"]["paper_fills_created"] == 0


def test_phase3aj_paper_funnel_replay_is_read_only(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(session, probability="0.50", best_price="0.60")

        payload = build_paper_trade_funnel(session, settings=settings, replay_readonly=True)

    assert payload["replay_readonly"] is True
    assert payload["order_submission"] is False
    assert payload["summary"]["paper_orders_created"] == 0


def test_phase3aj_paper_funnel_classifies_expired_crypto_window(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranked_market(
            session,
            probability="0.90",
            best_price="0.10",
            ticker="KXBTC-20JUL0108-B62750",
        )

        payload = build_paper_trade_funnel(session, settings=settings, replay_readonly=True)

    assert payload["reason_counts"]["EXPIRED_CRYPTO_WINDOW"] == 1
    assert payload["reason_counts"].get("QUOTE_STALE", 0) == 0
    assert payload["summary"]["tradeable_paper_only"] == 0
    assert payload["rows"][0]["crypto_window_expired"] is True
    assert payload["rows"][0]["decision_label"] == "NO_SIGNAL"
    stage_counts = {row["stage"]: row for row in payload["stage_counts"]}
    assert stage_counts["positive_raw_ev"]["pass_count"] == 0
    assert stage_counts["positive_executable_ev"]["pass_count"] == 0


def test_phase3aj_composite_apply_requires_backup_and_scope(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        with pytest.raises(ValueError):
            build_composite_settlement_resolve(
                session,
                paper_only=True,
                legacy_only=True,
                max_records=5,
                apply=True,
                backup_first=False,
            )


def test_phase3aj_source_readiness_keeps_flightaware_review_gated(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_source_readiness_report(
            session,
            sources=["flightaware", "usda", "cushman"],
        )

    rows = {row["source"]: row for row in payload["sources"]}
    assert rows["flightaware"]["state"] == "READY_FOR_REVIEW"
    assert rows["flightaware"]["link_safe"] is False
    assert rows["flightaware"]["forecast_safe"] is False
    assert rows["usda"]["forecast_safe"] is False
    assert rows["cushman"]["forecast_safe"] is False


def test_phase3aj_market_refresh_refuses_active_writer(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)

    def fake_monitor(*, settings=None):
        return {
            "current_writer_pid": 123,
            "current_writer_command": "kalshi-bot phase3bc-r5-crypto-freshness-watch",
            "current_writer_elapsed_seconds": 42,
            "safe_to_start_write": False,
            "recommended_next_action": "wait",
            "status": "WRITER_ACTIVE",
            "long_job_status": {},
        }

    monkeypatch.setattr("kalshi_predictor.phase3aj_gap_closure.db_writer_monitor", fake_monitor)
    with session_factory() as session:
        payload = build_market_data_refresh_status(session, settings=settings)

    assert payload["state"] == "BLOCKED_BY_ACTIVE_WRITER"
    assert payload["active_writer"]["writer_name"] == "crypto_watcher"
    assert payload["refresh_started"] is False


def test_phase3aj_golden_trace_is_fixture_only(tmp_path) -> None:
    settings = _settings(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_golden_trace_report(session, settings=settings)

    assert payload["trace_source"] == "fixture_contract_not_live_market_data"
    assert payload["live_or_demo_execution"] is False
    assert payload["paper_orders_created"] == 0
    assert payload["paper_fills_created"] == 0
    assert payload["positive_trace"][-1]["stage"] == "report_evidence"


def test_phase3aj_cli_commands_are_registered() -> None:
    runner = CliRunner()

    for command in (
        "gap-closure-doctor",
        "paper-trade-funnel",
        "composite-settlement-resolve",
        "source-readiness-report",
        "market-data-refresh",
        "phase-3aj-report",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert command in result.output


def _seed_ranked_market(
    session,
    *,
    probability: str,
    best_price: str,
    ticker: str = "KXTEST-3AJ",
) -> None:
    now = utc_now()
    session.add(
        Market(
            ticker=ticker,
            event_ticker=f"{ticker}-EVENT",
            series_ticker="KXTEST",
            title="Phase 3AJ test market",
            status="open",
            close_time=now + timedelta(hours=2),
            raw_json=encode_json({"ticker": ticker}),
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    snapshot = MarketSnapshot(
        ticker=ticker,
        captured_at=now,
        status="open",
        best_yes_bid="0.58",
        best_yes_ask="0.60",
        best_no_bid="0.40",
        best_no_ask="0.42",
        spread="0.02",
        raw_market_json=encode_json({"ticker": ticker}),
        raw_orderbook_json=encode_json(
            {"orderbook_fp": {"yes_dollars": [["0.58", "10"]], "no_dollars": [["0.40", "10"]]}}
        ),
    )
    session.add(snapshot)
    forecast = Forecast(
        ticker=ticker,
        forecasted_at=now,
        model_name="test_model",
        yes_probability=probability,
        market_mid_probability="0.59",
        best_yes_bid="0.58",
        best_yes_ask=best_price,
        feature_json=encode_json({"source": "test"}),
    )
    session.add(forecast)
    session.flush()
    session.add(
        MarketRanking(
            ticker=ticker,
            ranked_at=now,
            title="Phase 3AJ test market",
            status="open",
            series_ticker="KXTEST",
            event_ticker=f"{ticker}-EVENT",
            volume="100",
            open_interest="100",
            liquidity="1000",
            spread="0.02",
            midpoint="0.59",
            time_to_close_minutes="120",
            forecast_model="test_model",
            forecast_probability=probability,
            best_side=BUY_YES,
            best_price=best_price,
            estimated_edge="-0.10",
            liquidity_score="80",
            spread_score="90",
            time_score="80",
            model_confidence_score="80",
            opportunity_score="75",
            reason="test",
            raw_json=encode_json({"forecast_id": forecast.id}),
        )
    )
    session.commit()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kalshi_db_url=f"sqlite:///{Path(tmp_path) / 'phase3aj.db'}",
        opportunity_min_edge=Decimal("0.03"),
        opportunity_min_score=Decimal("60"),
        opportunity_max_spread=Decimal("0.10"),
    )


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3aj.db'}")
    return get_session_factory(engine)
