from datetime import UTC, datetime

from sqlalchemy import select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    get_forecasts_with_settlements,
    insert_forecast,
    insert_market_snapshot,
    upsert_market,
    upsert_settlement,
)
from kalshi_predictor.data.schema import Forecast, Market, MarketSnapshot, Settlement
from kalshi_predictor.forecasting.base import ForecastOutput


def test_repositories_init_and_insert_upsert(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'phase1.db'}"
    engine = init_db(db_url)
    session_factory = get_session_factory(engine)
    market_json = {
        "ticker": "TEST",
        "event_ticker": "EVENT",
        "series_ticker": "SERIES",
        "title": "Test market",
        "status": "open",
        "yes_bid_dollars": "0.30",
        "yes_ask_dollars": "0.34",
        "last_price_dollars": "0.32",
    }

    with session_factory() as session:
        upsert_market(session, market_json)
        upsert_market(session, {**market_json, "title": "Updated"})
        snapshot = insert_market_snapshot(
            session,
            market_json,
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.31", "10"]],
                    "no_dollars": [["0.65", "5"]],
                }
            },
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        insert_forecast(
            session,
            ForecastOutput(
                ticker="TEST",
                forecasted_at=snapshot.captured_at,
                model_name="market_implied_v1",
                yes_probability=snapshot.best_yes_bid,
                market_mid_probability=None,
                best_yes_bid=snapshot.best_yes_bid,
                best_yes_ask=snapshot.best_yes_ask,
                feature_json={"source": "test"},
            ),
        )
        upsert_settlement(
            session,
            {
                "ticker": "TEST",
                "status": "settled",
                "result": "yes",
                "settlement_ts": "2026-01-02T00:00:00Z",
            },
        )
        session.commit()

    with session_factory() as session:
        assert len(session.scalars(select(Market)).all()) == 1
        assert session.scalar(select(Market).where(Market.ticker == "TEST")).ticker == "TEST"
        assert len(session.scalars(select(MarketSnapshot)).all()) == 1
        assert len(session.scalars(select(Forecast)).all()) == 1
        assert len(session.scalars(select(Settlement)).all()) == 1
        joined = get_forecasts_with_settlements("market_implied_v1", session=session)
        assert len(joined) == 1
