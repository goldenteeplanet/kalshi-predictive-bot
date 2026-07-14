from datetime import timedelta
from decimal import Decimal

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_settlement,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.leaderboard.builder import build_model_leaderboard
from kalshi_predictor.utils.time import utc_now


def test_leaderboard_includes_models_with_no_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = build_model_leaderboard(session, days=30, persist=False)

    names = {row["model_name"] for row in result.rows}
    assert "weather_v1" in names
    assert next(row for row in result.rows if row["model_name"] == "weather_v1")[
        "forecast_count"
    ] == 0


def test_leaderboard_computes_forecast_counts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_evaluated_forecast(session)

        result = build_model_leaderboard(session, days=30, persist=False)

    row = next(row for row in result.rows if row["model_name"] == "market_implied_v1")
    assert row["forecast_count"] == 1
    assert row["evaluated_forecast_count"] == 1
    assert row["brier_score"] is not None


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'leaderboard.db'}")
    return get_session_factory(engine)


def _seed_evaluated_forecast(session) -> None:
    now = utc_now() - timedelta(days=1)
    insert_market_snapshot(
        session,
        {
            "ticker": "LEADER",
            "status": "settled",
            "title": "Leader test",
            "volume_fp": "10",
            "open_interest_fp": "5",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "10"]],
            }
        },
        now,
    )
    insert_forecast(
        session,
        ForecastOutput(
            ticker="LEADER",
            forecasted_at=now,
            model_name="market_implied_v1",
            yes_probability=Decimal("0.70"),
            market_mid_probability=None,
            best_yes_bid=Decimal("0.40"),
            best_yes_ask=Decimal("0.50"),
            feature_json={"source": "test"},
        ),
    )
    upsert_settlement(session, {"ticker": "LEADER", "result": "yes"})
    session.flush()
