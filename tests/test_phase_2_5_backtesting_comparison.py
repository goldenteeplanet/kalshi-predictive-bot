from datetime import timedelta
from decimal import Decimal

from kalshi_predictor.backtesting.engine import run_backtest
from kalshi_predictor.comparison.reports import generate_strategy_comparison_report
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_settlement,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.utils.time import utc_now


def test_backtest_engine_ignores_unsettled_markets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast(session, ticker="UNSETTLED", yes_probability=Decimal("0.60"))

        result = run_backtest(session, model_name="market_implied_v1", days=30, persist=False)

        assert result.forecasts_scanned == 1
        assert result.evaluated_forecasts == 0
        assert result.summary["total_trades"] == 0


def test_backtest_calculates_pnl_correctly(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast(session, ticker="SETTLED_YES", yes_probability=Decimal("0.60"))
        upsert_settlement(session, {"ticker": "SETTLED_YES", "result": "yes"})

        result = run_backtest(session, model_name="market_implied_v1", days=30, persist=False)

        assert result.summary["total_trades"] == 1
        assert result.trades[0]["side"] == "BUY_YES"
        assert result.trades[0]["pnl"] == "0.50"
        assert result.summary["total_pnl"] == "0.50"


def test_strategy_comparison_report_handles_missing_models(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = tmp_path / "comparison.md"
    with session_factory() as session:
        generate_strategy_comparison_report(session, days=30, output_path=output)

    text = output.read_text(encoding="utf-8")
    assert "market_implied_v1" in text
    assert "No forecasts found for this model." in text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase25_backtest.db'}")
    return get_session_factory(engine)


def _seed_forecast(session, *, ticker: str, yes_probability: Decimal):
    now = utc_now() - timedelta(days=1)
    insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "settled",
            "title": "Test market",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "8"]],
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
            yes_probability=yes_probability,
            market_mid_probability=None,
            best_yes_bid=Decimal("0.40"),
            best_yes_ask=Decimal("0.50"),
            feature_json={"source": "test"},
        ),
    )
    session.flush()

