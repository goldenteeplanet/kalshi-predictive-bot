from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import MarketOpportunity
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.opportunities.scoring import (
    calculate_opportunity_score,
    score_liquidity,
    score_spread,
    score_time_to_close,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.utils.time import utc_now


def test_liquidity_scoring_handles_missing_values() -> None:
    assert score_liquidity(volume=None, open_interest=None, liquidity=None) == Decimal("0.00")


def test_spread_scoring_rewards_tighter_spreads() -> None:
    assert score_spread("0.01") > score_spread("0.08")


def test_time_scoring_penalizes_near_expiration_markets() -> None:
    assert score_time_to_close("5") < score_time_to_close("240")


def test_opportunity_score_combines_components() -> None:
    score = calculate_opportunity_score(
        estimated_edge=Decimal("0.10"),
        liquidity_score=Decimal("50"),
        spread_score=Decimal("80"),
        time_score=Decimal("70"),
        model_confidence_score=Decimal("60"),
    )

    assert score == Decimal("59.50")


def test_scanner_detects_buy_yes_opportunity(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_market(
            session,
            ticker="BUY_YES_MARKET",
            probability=Decimal("0.70"),
            yes_bid="0.40",
            no_bid="0.50",
        )

        summary = scan_opportunities(
            session,
            settings=_settings(),
            min_edge=Decimal("0.03"),
            min_score=Decimal("10"),
        )

        assert summary.opportunities_detected == 1
        assert summary.opportunities[0]["side"] == BUY_YES
        assert session.scalar(select(MarketOpportunity)).side == BUY_YES


def test_scanner_detects_buy_no_opportunity(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_market(
            session,
            ticker="BUY_NO_MARKET",
            probability=Decimal("0.20"),
            yes_bid="0.60",
            no_bid="0.35",
        )

        summary = scan_opportunities(
            session,
            settings=_settings(),
            min_edge=Decimal("0.03"),
            min_score=Decimal("10"),
        )

        assert summary.opportunities_detected == 1
        assert summary.opportunities[0]["side"] == BUY_NO


def test_scanner_skips_low_edge_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_market(
            session,
            ticker="LOW_EDGE",
            probability=Decimal("0.51"),
            yes_bid="0.45",
            no_bid="0.50",
        )

        summary = scan_opportunities(
            session,
            settings=_settings(),
            min_edge=Decimal("0.03"),
            min_score=Decimal("10"),
        )

        assert summary.rankings_inserted == 1
        assert summary.opportunities_detected == 0


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'opportunities.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        opportunity_min_edge=Decimal("0.03"),
        opportunity_min_score=Decimal("10"),
        opportunity_max_spread=Decimal("0.20"),
        opportunity_min_liquidity=Decimal("0"),
        opportunity_min_time_to_close_minutes=Decimal("30"),
        opportunity_max_results=20,
    )


def _seed_market(
    session,
    *,
    ticker: str,
    probability: Decimal,
    yes_bid: str,
    no_bid: str,
):
    now = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": ticker,
            "series_ticker": "SERIES",
            "event_ticker": "EVENT",
            "close_time": (now + timedelta(hours=4)).isoformat(),
            "volume_fp": "1000",
            "open_interest_fp": "500",
            "liquidity_dollars": "10000",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [[yes_bid, "10"]],
                "no_dollars": [[no_bid, "10"]],
            }
        },
        now,
    )
    insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=now,
            model_name="market_implied_v1",
            yes_probability=probability,
            market_mid_probability=None,
            best_yes_bid=Decimal(yes_bid),
            best_yes_ask=Decimal(snapshot.best_yes_ask),
            feature_json={"source": "test"},
        ),
    )
    session.flush()

