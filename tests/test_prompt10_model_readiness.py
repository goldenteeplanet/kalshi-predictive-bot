from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.crypto.repository import insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import ForecastSkipLog
from kalshi_predictor.forecasting.crypto_v2 import CryptoV2Forecaster
from kalshi_predictor.forecasting.economic_v1 import EconomicV1Forecaster
from kalshi_predictor.forecasting.registry import MODEL_NAMES, get_forecaster
from kalshi_predictor.forecasting.skip_log import log_forecast_skip
from kalshi_predictor.forecasting.weather_v2 import WeatherV2Forecaster
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now

EXPECTED_MODELS = {
    "market_implied_v1",
    "crypto_v2",
    "weather_v2",
    "economic_v1",
    "ensemble_v1",
    "ensemble_v2",
}


def test_all_expected_models_are_registered() -> None:
    assert EXPECTED_MODELS.issubset(set(MODEL_NAMES))
    for model_name in EXPECTED_MODELS:
        assert get_forecaster(model_name) is not None


def test_forecast_skip_log_insert_works(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        row = log_forecast_skip(
            session,
            model_name="crypto_v2",
            ticker="SKIP-TEST",
            reason="no crypto features",
            required_data=["crypto features"],
            available_data={"link": True},
        )
        loaded = session.scalar(select(ForecastSkipLog).where(ForecastSkipLog.id == row.id))

    assert loaded is not None
    assert loaded.reason == "no crypto features"


def test_crypto_v2_logs_skip_when_no_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="BTC-SKIP", title="Will Bitcoin go above 100k?")
        insert_crypto_market_link(
            session,
            ticker=snapshot.ticker,
            symbol="BTC",
            confidence=Decimal("1.0"),
            reason="test link",
        )

        assert CryptoV2Forecaster().forecast(session, snapshot) is None
        skip = session.scalar(
            select(ForecastSkipLog).where(ForecastSkipLog.model_name == "crypto_v2")
        )

    assert skip is not None
    assert skip.reason == "no crypto features"


def test_weather_v2_logs_skip_when_no_link(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="WX-SKIP", title="Will rain exceed 1 inch?")

        assert WeatherV2Forecaster().forecast(session, snapshot) is None
        skip = session.scalar(
            select(ForecastSkipLog).where(ForecastSkipLog.model_name == "weather_v2")
        )

    assert skip is not None
    assert skip.reason == "no weather market link"


def test_economic_v1_logs_skip_when_no_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="CPI-SKIP", title="Will CPI exceed 3%?")

        assert EconomicV1Forecaster().forecast(session, snapshot) is None
        skip = session.scalar(
            select(ForecastSkipLog).where(ForecastSkipLog.model_name == "economic_v1")
        )

    assert skip is not None
    assert skip.reason == "no economic market link"


def test_model_health_lists_inactive_models(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'health.db'}"

    result = runner.invoke(app, ["model-health"], env={"KALSHI_DB_URL": db_url})

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "Inactive models" in result.output
    assert "crypto_v2" in result.output


def test_models_status_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'status.db'}"

    result = runner.invoke(app, ["models-status"], env={"KALSHI_DB_URL": db_url})

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "model=market_implied_v1" in result.output
    assert "required=" in result.output


def test_ui_model_page_shows_next_action_for_inactive_model(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    client = TestClient(create_app(session_factory=session_factory))

    response = client.get("/models")

    assert response.status_code == 200
    assert "Run ingest-crypto, build-crypto-features, link-crypto-markets." in response.text
    assert "Load economic sample data or connect economic calendar ingestion." in response.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'prompt10.db'}")
    return get_session_factory(engine)


def _seed_snapshot(session, *, ticker: str, title: str):
    captured_at = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": title,
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.55",
            "last_price_dollars": "0.50",
            "close_time": (captured_at + timedelta(hours=4)).isoformat(),
        },
        {"orderbook": {"yes": [[45, 10]], "no": [[45, 10]]}},
        captured_at,
    )
    session.flush()
    return snapshot
