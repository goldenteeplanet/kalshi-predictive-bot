from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.paper.ledger import create_paper_order
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.paper.strategy import generate_paper_decisions


def test_strategy_creates_buy_yes_when_edge_above_threshold(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast(session, ticker="YES_EDGE", yes_probability=Decimal("0.60"))

        result = generate_paper_decisions(session, settings=_settings())

        assert result.decisions_generated == 1
        assert result.decisions[0].side == BUY_YES
        assert result.decisions[0].edge == Decimal("0.10")


def test_strategy_creates_buy_no_when_enabled_and_edge_above_threshold(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast(session, ticker="NO_EDGE", yes_probability=Decimal("0.30"))

        result = generate_paper_decisions(session, settings=_settings())

    assert result.decisions_generated == 1
    assert result.decisions[0].side == BUY_NO
    assert result.decisions[0].edge == Decimal("0.18")


def test_strategy_skips_when_edge_below_threshold(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast(session, ticker="NO_EDGE", yes_probability=Decimal("0.52"))

        result = generate_paper_decisions(session, settings=_settings())

        assert result.decisions_generated == 0
        assert result.skipped_due_to_edge == 1


def test_strategy_does_not_create_duplicate_order_for_same_forecast(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        forecast = _seed_forecast(session, ticker="DUPLICATE", yes_probability=Decimal("0.60"))
        decision = generate_paper_decisions(session, settings=_settings()).decisions[0]
        create_paper_order(session, decision)
        session.commit()

        result = generate_paper_decisions(session, settings=_settings())

        assert forecast.id is not None
        assert result.decisions_generated == 0
        assert result.duplicates_skipped == 1
        assert len(session.scalars(select(PaperOrder)).all()) == 1


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'paper_strategy.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        learning_mode=False,
        paper_min_edge=Decimal("0.05"),
        paper_max_order_quantity=1,
        paper_max_position_per_market=5,
        paper_max_open_orders=100,
        paper_allow_buy_no=True,
    )


def _seed_forecast(session, *, ticker: str, yes_probability: Decimal):
    market_json = {
        "ticker": ticker,
        "status": "open",
        "yes_bid_dollars": "0.48",
        "yes_ask_dollars": "0.50",
        "last_price_dollars": "0.49",
    }
    insert_market_snapshot(
        session,
        market_json,
        {
            "orderbook_fp": {
                "yes_dollars": [["0.48", "1"]],
                "no_dollars": [["0.50", "1"]],
            }
        },
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            model_name="market_implied_v1",
            yes_probability=yes_probability,
            market_mid_probability=None,
            best_yes_bid=Decimal("0.48"),
            best_yes_ask=Decimal("0.50"),
            feature_json={"source": "test"},
        ),
    )
    session.flush()
    return forecast
