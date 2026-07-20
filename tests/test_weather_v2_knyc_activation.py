from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import func, select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import decode_json, insert_market_snapshot
from kalshi_predictor.data.schema import WeatherFeature, WeatherObservation
from kalshi_predictor.forecasting.weather_v2 import WeatherV2Forecaster
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.features import build_weather_features
from kalshi_predictor.weather.ingestion import ingest_weather_location
from kalshi_predictor.weather.providers import WeatherFetchResult
from kalshi_predictor.weather.repository import (
    insert_weather_features,
    insert_weather_forecast,
    insert_weather_market_link,
)

TICKER = "KXTEMPNYCH-26JUL1523-T80.99"
TARGET = datetime(2026, 7, 16, 3, tzinfo=timezone.utc)


def test_flag_off_preserves_baseline_weather_v2_behavior(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_exact_runtime_rows(session)
        forecast = WeatherV2Forecaster(settings=_settings(enabled=False)).forecast(
            session, snapshot
        )

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.44005")
    assert "knyc_temperature_probability" not in forecast.feature_json
    assert forecast.notes == "weather_v2 midpoint plus bounded weather adjustment."


def test_flag_on_applies_exact_bounded_probability_helpers(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_exact_runtime_rows(session)
        forecast = WeatherV2Forecaster(settings=_settings(enabled=True)).forecast(
            session, snapshot
        )

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.55")
    evidence = forecast.feature_json["knyc_temperature_probability"]
    assert evidence["status"] == "APPLIED"
    assert evidence["applied"] is True
    assert evidence["thresholds_changed"] is False
    assert evidence["bounded_adjustment"] == "0.10"
    assert evidence["helpers"] == [
        "sigma_for_lead_time",
        "probability_above",
        "probability_above_with_observed_max",
    ]


def test_flag_on_keeps_baseline_when_exact_evidence_is_invalid(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_exact_runtime_rows(session, station_id="KLGA")
        forecast = WeatherV2Forecaster(settings=_settings(enabled=True)).forecast(
            session, snapshot
        )

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.44005")
    evidence = forecast.feature_json["knyc_temperature_probability"]
    assert evidence["status"] == "BLOCKED"
    assert evidence["applied"] is False
    assert evidence["blocker"] == "STATION_NOT_KNYC"


def test_feature_builder_attaches_only_exact_knyc_observation(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        insert_weather_forecast(
            session,
            location_key="new_york",
            source="noaa",
            forecast_generated_at=now - timedelta(hours=1),
            forecast_time=now,
            temperature_f="79",
        )
        observation = WeatherObservation(
            location_key="new_york",
            source="noaa_nws_observation_non_settlement_evidence",
            observed_at=now + timedelta(minutes=4),
            temperature_f="80",
            raw_json=(
                '{"station_id":"KNYC","evidence_role":'
                '"NON_SETTLEMENT_POINT_OBSERVATION","settlement_source":'
                '"the_weather_company"}'
            ),
            created_at=now,
        )
        session.add(observation)
        session.flush()

        summary = build_weather_features(
            session,
            location_key="new_york",
            settings=_settings(enabled=True),
        )
        feature = session.scalar(select(WeatherFeature))

    assert summary.features_inserted == 1
    assert feature is not None
    evidence = decode_json(feature.raw_json)["knyc_observation_evidence"]
    assert evidence["id"] == observation.id
    assert evidence["offset_seconds"] == 240
    assert evidence["target_utc_time"] == now.isoformat()


def test_enabled_new_york_ingest_deduplicates_station_observations(
    tmp_path, monkeypatch
) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    payload = {
        "features": [
            {
                "properties": {
                    "station": "https://api.weather.gov/stations/KNYC",
                    "timestamp": now.isoformat(),
                    "temperature": {"unitCode": "wmoUnit:degC", "value": 25},
                }
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        "kalshi_predictor.weather.ingestion.fetch_noaa_hourly_forecast",
        lambda **_: WeatherFetchResult(source="noaa", forecasts=[], errors=[]),
    )
    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.weather.gov"
    ) as client:
        with session_factory() as session:
            first = ingest_weather_location(
                session,
                location_key="new_york",
                latitude=40.7128,
                longitude=-74.0060,
                settings=_settings(enabled=True),
                station_client=client,
            )
            second = ingest_weather_location(
                session,
                location_key="new_york",
                latitude=40.7128,
                longitude=-74.0060,
                settings=_settings(enabled=True),
                station_client=client,
            )
            count = session.scalar(select(func.count()).select_from(WeatherObservation))

    assert first.observations_inserted == 1
    assert second.observations_inserted == 0
    assert count == 1


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'knyc_activation.db'}")
    return get_session_factory(engine)


def _settings(*, enabled: bool) -> Settings:
    return Settings(
        weather_v2_max_adjustment=Decimal("0.10"),
        weather_v2_min_link_confidence=Decimal("0.6"),
        weather_v2_max_forecast_age_hours=24,
        weather_v2_default_location_key="new_york",
        weather_v2_knyc_observation_enabled=enabled,
    )


def _seed_exact_runtime_rows(session, *, station_id: str = "KNYC"):
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": TICKER,
            "series_ticker": "KXTEMPNYCH",
            "event_ticker": "KXTEMPNYCH-26JUL1523",
            "status": "open",
            "strike_type": "greater",
            "floor_strike": 80.99,
            "cap_strike": None,
            "close_time": TARGET.isoformat(),
            "rules_primary": (
                "The Weather Company reports temperature for coordinates KNYC."
            ),
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "10"]],
            }
        },
        utc_now(),
    )
    insert_weather_market_link(
        session,
        ticker=TICKER,
        location_key="new_york",
        weather_metric="TEMPERATURE",
        target_operator="ABOVE",
        target_value="80.99",
        target_time=TARGET,
        confidence="1.0",
        reason="exact test link",
    )
    insert_weather_features(
        session,
        location_key="new_york",
        source="test",
        generated_at=utc_now(),
        target_time=TARGET,
        features={
            "temperature_f": "79",
            "forecast_age_hours": "1",
            "forecast_generated_at": "2026-07-15T21:00:00+00:00",
            "knyc_observation_evidence": {
                "table": "weather_observations",
                "id": 9,
                "station_id": station_id,
                "evidence_role": "NON_SETTLEMENT_POINT_OBSERVATION",
                "settlement_source": "the_weather_company",
                "target_utc_time": TARGET.isoformat(),
                "offset_seconds": 240,
                "observation_temperature_f": "82",
            },
        },
    )
    return snapshot
