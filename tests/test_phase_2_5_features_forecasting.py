from datetime import timedelta
from decimal import Decimal

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.external.weather import ingest_weather_json
from kalshi_predictor.features.builder import build_feature_snapshot
from kalshi_predictor.features.market_features import build_market_features
from kalshi_predictor.features.repository import (
    feature_payload,
    get_latest_features_for_ticker,
)
from kalshi_predictor.forecasting.crypto_v1 import CryptoV1Forecaster
from kalshi_predictor.forecasting.economic_v1 import EconomicV1Forecaster
from kalshi_predictor.forecasting.ensemble_v1 import EnsembleV1Forecaster
from kalshi_predictor.forecasting.weather_v1 import WeatherV1Forecaster
from kalshi_predictor.utils.time import utc_now


def test_feature_builder_creates_market_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="FEATURES")

        features = build_market_features(snapshot)

        assert features["best_yes_bid"] == "0.40"
        assert features["best_yes_ask"] == "0.50"
        assert features["spread"] == "0.10"
        assert features["midpoint"] == "0.45"
        assert features["market_status"] == "open"
        assert features["series_ticker"] == "SERIES"


def test_external_ingestion_stores_json(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ingest_weather_json(
            session,
            {"ticker": "WEATHER", "features": {"yes_probability": "0.72", "temp_f": 31}},
        )
        session.commit()

        record = get_latest_features_for_ticker(session, "WEATHER", feature_set_name="weather")
        payload = feature_payload(record)

        assert payload["yes_probability"] == "0.72"
        assert payload["temp_f"] == 31


def test_weather_v1_skips_cleanly_when_no_weather_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="NO_WEATHER")
        build_feature_snapshot(session, snapshot)

        assert WeatherV1Forecaster().forecast(session, snapshot) is None


def test_crypto_v1_skips_cleanly_when_market_is_not_crypto(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="NOT_CRYPTO", title="Will it rain tomorrow?")
        build_feature_snapshot(session, snapshot)

        assert CryptoV1Forecaster().forecast(session, snapshot) is None


def test_economic_v1_skips_cleanly_when_no_economic_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="CPI", title="Will CPI exceed 3%?")
        build_feature_snapshot(session, snapshot)

        assert EconomicV1Forecaster().forecast(session, snapshot) is None


def test_ensemble_v1_averages_available_model_forecasts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(session, ticker="ENSEMBLE")
        ingest_weather_json(
            session,
            {"ticker": "ENSEMBLE", "features": {"yes_probability": "0.80"}},
        )
        build_feature_snapshot(session, snapshot)

        forecast = EnsembleV1Forecaster().forecast(session, snapshot)

        assert forecast is not None
        assert forecast.model_name == "ensemble_v1"
        assert forecast.yes_probability == Decimal("0.625")
        assert forecast.feature_json["component_count"] == 2


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase25_features.db'}")
    return get_session_factory(engine)


def _seed_snapshot(session, *, ticker: str, title: str = "Will it rain?"):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": title,
            "series_ticker": "SERIES",
            "event_ticker": "EVENT",
            "close_time": (now + timedelta(hours=2)).isoformat(),
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
            "volume_fp": "100",
            "open_interest_fp": "20",
            "liquidity_dollars": "1000",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "8"]],
            }
        },
        now,
    )

