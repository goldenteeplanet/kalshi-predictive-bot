from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import MarketLeg, WeatherMarketLink
from kalshi_predictor.forecasting.weather_v2 import WeatherV2Forecaster
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.features import (
    build_weather_features,
    freeze_risk_score,
    rain_risk_score,
    wind_risk_score,
)
from kalshi_predictor.weather.ingestion import ingest_manual_weather_json
from kalshi_predictor.weather.linker import detect_weather_market, link_weather_markets
from kalshi_predictor.weather.providers import parse_noaa_hourly_forecast
from kalshi_predictor.weather.reports import generate_weather_backtest_report
from kalshi_predictor.weather.repository import (
    get_weather_features,
    get_weather_forecasts,
    insert_weather_features,
    insert_weather_forecast,
    insert_weather_market_link,
)


def test_noaa_provider_parser_handles_sample_forecast_json() -> None:
    periods = parse_noaa_hourly_forecast(
        location_key="Kansas City",
        latitude="39.0997",
        longitude="-94.5786",
        payload=_sample_noaa_payload(),
    )

    assert len(periods) == 2
    assert periods[0].location_key == "kansas_city"
    assert periods[0].temperature_f == Decimal("72")
    assert periods[0].precipitation_probability == Decimal("60")
    assert periods[0].wind_speed_mph == Decimal("20")


def test_manual_weather_ingestion_stores_json(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        summary = ingest_manual_weather_json(
            session,
            _sample_noaa_payload(),
            location_key="kansas_city",
        )

        forecasts = get_weather_forecasts(session, "kansas_city")

        assert summary.forecasts_inserted == 2
        assert len(forecasts) == 2
        assert forecasts[0].short_forecast == "Chance Showers"


def test_weather_feature_builder_handles_missing_fields(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        insert_weather_forecast(
            session,
            location_key="kansas_city",
            source="test",
            forecast_generated_at=utc_now(),
            forecast_time=utc_now() + timedelta(hours=1),
        )

        summary = build_weather_features(session, location_key="kansas_city")
        features = get_weather_features(session, "kansas_city")

        assert summary.features_inserted == 1
        assert features[0].freeze_risk_score is None
        assert "Missing temperature" in features[0].raw_json


def test_weather_rain_risk_scoring_works() -> None:
    assert rain_risk_score(Decimal("80"), Decimal("0.5")) > Decimal("0.7")


def test_weather_freeze_risk_scoring_works() -> None:
    assert freeze_risk_score(Decimal("30")) == Decimal("1")
    assert freeze_risk_score(Decimal("36")) == Decimal("0.5")
    assert freeze_risk_score(Decimal("45")) == Decimal("0")


def test_weather_wind_risk_scoring_works() -> None:
    assert wind_risk_score(Decimal("20"), Decimal("60")) > Decimal("0.7")


def test_weather_linker_detects_temperature_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {"ticker": "WX-TEMP", "title": "Will Kansas City temperature exceed 70 degrees?"},
        )

        detection = detect_weather_market(market)

        assert detection.weather_metric == "TEMPERATURE"
        assert detection.location_key == "kansas_city"
        assert detection.target_operator == "ABOVE"
        assert detection.target_value == Decimal("70")
        assert detection.confidence == Decimal("1.0")


def test_weather_linker_prefers_operator_threshold_over_time_fields(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXTEMPNYCH-26JUN2703-T61.99",
                "event_ticker": "KXTEMPNYCH-26JUN2703",
                "title": (
                    "Will the temp in New York City be above 61.99° "
                    "on Jun 27, 2026 at 3am EDT?"
                ),
            },
        )

        detection = detect_weather_market(market)

        assert detection.weather_metric == "TEMPERATURE"
        assert detection.location_key == "new_york"
        assert detection.target_operator == "ABOVE"
        assert detection.target_value == Decimal("61.99")


def test_weather_linker_scans_parsed_weather_candidates_first(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        captured_at = utc_now()
        market = upsert_market(
            session,
            {
                "ticker": "KXTEMPNYCH-26JUN2703-T61.99",
                "event_ticker": "KXTEMPNYCH-26JUN2703",
                "title": (
                    "Will the temp in New York City be above 61.99° "
                    "on Jun 27, 2026 at 3am EDT?"
                ),
            },
        )
        upsert_market(
            session,
            {"ticker": "SPORTS-NOISE", "title": "Will Team A beat Team B?"},
        )
        session.add(
            MarketLeg(
                ticker=market.ticker,
                leg_index=1,
                parsed_at=captured_at,
                side="YES",
                category="weather",
                market_type="THRESHOLD",
                entity_name="New York City temperature",
                operator="ABOVE",
                threshold_value="61.99",
                unit="F",
                confidence="1.0",
                raw_text=market.title or "",
                reason="parsed weather fixture",
                raw_json="{}",
            )
        )

        result = link_weather_markets(session)
        link = session.scalar(select(WeatherMarketLink))

        assert result.markets_scanned == 1
        assert result.links_created == 1
        assert link is not None
        assert link.ticker == market.ticker
        assert link.location_key == "new_york"
        assert link.target_value == "61.99"


def test_weather_linker_scans_kalshi_weather_family_without_parsed_leg(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXTEMPNYCH-26JUL0113-T81.99",
                "event_ticker": "KXTEMPNYCH-26JUL0113",
                "series_ticker": "KXTEMPNYCH",
                "status": "active",
                "title": (
                    "Will the temp in New York City be above 81.99° "
                    "on Jul 1, 2026 at 1pm EDT?"
                ),
            },
        )
        upsert_market(
            session,
            {"ticker": "SPORTS-NOISE", "title": "Will Team A beat Team B?"},
        )

        result = link_weather_markets(session)
        link = session.scalar(select(WeatherMarketLink))

        assert result.markets_scanned == 1
        assert result.links_created == 1
        assert link is not None
        assert link.ticker == market.ticker
        assert link.location_key == "new_york"
        assert link.weather_metric == "TEMPERATURE"
        assert link.target_value == "81.99"


def test_weather_linker_is_idempotent_for_existing_links(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        captured_at = utc_now()
        market = upsert_market(
            session,
            {
                "ticker": "KXTEMPNYCH-26JUN2703-T61.99",
                "event_ticker": "KXTEMPNYCH-26JUN2703",
                "title": (
                    "Will the temp in New York City be above 61.99° "
                    "on Jun 27, 2026 at 3am EDT?"
                ),
            },
        )
        session.add(
            MarketLeg(
                ticker=market.ticker,
                leg_index=1,
                parsed_at=captured_at,
                side="YES",
                category="weather",
                market_type="THRESHOLD",
                entity_name="New York City temperature",
                operator="ABOVE",
                threshold_value="61.99",
                unit="F",
                confidence="1.0",
                raw_text=market.title or "",
                reason="parsed weather fixture",
                raw_json="{}",
            )
        )

        first = link_weather_markets(session)
        second = link_weather_markets(session)
        link_count = session.scalar(
            select(func.count()).select_from(WeatherMarketLink).where(
                WeatherMarketLink.ticker == market.ticker
            )
        )

        assert first.links_created == 1
        assert second.links_created == 0
        assert link_count == 1


def test_weather_linker_detects_rain_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {"ticker": "WX-RAIN", "title": "Will it rain in New York today?"},
        )

        detection = detect_weather_market(market)

        assert detection.weather_metric == "RAIN"
        assert detection.location_key == "new_york"
        assert detection.confidence == Decimal("0.8")


def test_weather_linker_detects_wind_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {"ticker": "WX-WIND", "title": "Will Chicago wind gusts be above 40 mph?"},
        )

        detection = detect_weather_market(market)

        assert detection.weather_metric == "WIND"
        assert detection.location_key == "chicago"
        assert detection.target_value == Decimal("40")


def test_weather_linker_ignores_non_weather_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(session, {"ticker": "SPORTS", "title": "Will Team A win?"})

        detection = detect_weather_market(market)

        assert detection.weather_metric == "UNKNOWN"
        assert detection.confidence == Decimal("0.0")


def test_weather_v2_skips_without_link(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_weather_snapshot(session, title="Will Kansas City temp exceed 70?")

        assert WeatherV2Forecaster(settings=_settings()).forecast(session, snapshot) is None


def test_weather_v2_skips_without_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_weather_snapshot(session, title="Will Kansas City temp exceed 70?")
        insert_weather_market_link(
            session,
            ticker=snapshot.ticker,
            location_key="kansas_city",
            weather_metric="TEMPERATURE",
            target_operator="ABOVE",
            target_value="70",
            target_time=utc_now() + timedelta(hours=1),
            confidence="1.0",
            reason="test",
        )

        assert WeatherV2Forecaster(settings=_settings()).forecast(session, snapshot) is None


def test_weather_v2_adjusts_upward_when_temp_above_threshold_for_above_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_weather_snapshot(session, title="Will Kansas City temp exceed 70?")
        _seed_link_and_features(session, snapshot.ticker, operator="ABOVE", temperature="80")

        forecast = WeatherV2Forecaster(settings=_settings()).forecast(session, snapshot)

        assert forecast is not None
        assert forecast.yes_probability == Decimal("0.50")


def test_weather_v2_adjusts_downward_when_temp_above_threshold_for_below_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_weather_snapshot(session, title="Will Kansas City temp be below 70?")
        _seed_link_and_features(session, snapshot.ticker, operator="BELOW", temperature="80")

        forecast = WeatherV2Forecaster(settings=_settings()).forecast(session, snapshot)

        assert forecast is not None
        assert forecast.yes_probability == Decimal("0.40")


def test_weather_v2_uses_freshest_source_feature_for_target_time_ties(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_weather_snapshot(session, title="Will Kansas City temp exceed 70?")
        target_time = utc_now() + timedelta(hours=1)
        generated_at = utc_now()
        insert_weather_market_link(
            session,
            ticker=snapshot.ticker,
            location_key="kansas_city",
            weather_metric="TEMPERATURE",
            target_operator="ABOVE",
            target_value="70",
            target_time=target_time,
            confidence="1.0",
            reason="test",
        )
        _insert_weather_feature_for_tie_test(
            session,
            generated_at=generated_at,
            target_time=target_time,
            temperature="20",
            forecast_age_hours="83",
            forecast_generated_at=(generated_at - timedelta(hours=83)).isoformat(),
        )
        fresh_feature = _insert_weather_feature_for_tie_test(
            session,
            generated_at=generated_at,
            target_time=target_time,
            temperature="80",
            forecast_age_hours="1",
            forecast_generated_at=(generated_at - timedelta(hours=1)).isoformat(),
        )

        forecast = WeatherV2Forecaster(settings=_settings()).forecast(session, snapshot)

        assert forecast is not None
        assert forecast.feature_json["weather_feature_values"]["temperature_f"] == "80"
        assert forecast.feature_json["weather_feature_values"]["target_time"] == target_time.isoformat()
        assert fresh_feature.id is not None


def test_weather_backtest_handles_no_evaluated_trades(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = tmp_path / "weather_backtest.md"
    with session_factory() as session:
        path = generate_weather_backtest_report(session, days=30, output_path=output)

    text = path.read_text(encoding="utf-8")
    assert "Weather Backtest" in text
    assert "weather_v2" in text
    assert "0" in text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'weather_phase28.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        weather_v2_max_adjustment=Decimal("0.10"),
        weather_v2_min_link_confidence=Decimal("0.6"),
        weather_v2_max_forecast_age_hours=24,
        weather_v2_default_location_key="kansas_city",
    )


def _seed_weather_snapshot(session, *, title: str):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": "WX-KC-TEMP",
            "status": "open",
            "title": title,
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "10"]],
            }
        },
        now,
    )


def _seed_link_and_features(
    session,
    ticker: str,
    *,
    operator: str,
    temperature: str,
) -> None:
    target_time = utc_now() + timedelta(hours=1)
    insert_weather_market_link(
        session,
        ticker=ticker,
        location_key="kansas_city",
        weather_metric="TEMPERATURE",
        target_operator=operator,
        target_value="70",
        target_time=target_time,
        confidence="1.0",
        reason="test",
    )
    insert_weather_features(
        session,
        location_key="kansas_city",
        source="test",
        generated_at=utc_now(),
        target_time=target_time,
        features={
            "temperature_f": temperature,
            "precipitation_probability": "10",
            "expected_precipitation_inches": "0",
            "wind_speed_mph": "5",
            "wind_gust_mph": "8",
            "freeze_risk_score": "0",
            "rain_risk_score": "0.1",
            "wind_risk_score": "0.1",
            "weather_confidence_score": "0.9",
            "forecast_age_hours": "1",
        },
    )


def _insert_weather_feature_for_tie_test(
    session,
    *,
    generated_at,
    target_time,
    temperature: str,
    forecast_age_hours: str,
    forecast_generated_at: str,
):
    return insert_weather_features(
        session,
        location_key="kansas_city",
        source="test",
        generated_at=generated_at,
        target_time=target_time,
        features={
            "temperature_f": temperature,
            "precipitation_probability": "10",
            "expected_precipitation_inches": "0",
            "wind_speed_mph": "5",
            "wind_gust_mph": "8",
            "freeze_risk_score": "0",
            "rain_risk_score": "0.1",
            "wind_risk_score": "0.1",
            "weather_confidence_score": "0.9",
            "forecast_age_hours": forecast_age_hours,
            "forecast_generated_at": forecast_generated_at,
        },
    )


def _sample_noaa_payload():
    return {
        "latitude": "39.0997",
        "longitude": "-94.5786",
        "properties": {
            "generatedAt": "2026-06-16T18:00:00+00:00",
            "periods": [
                {
                    "number": 1,
                    "startTime": "2026-06-16T19:00:00+00:00",
                    "temperature": 72,
                    "temperatureUnit": "F",
                    "dewpoint": {"unitCode": "wmoUnit:degC", "value": 17.2},
                    "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 70},
                    "windSpeed": "10 to 20 mph",
                    "windGust": "25 mph",
                    "probabilityOfPrecipitation": {
                        "unitCode": "wmoUnit:percent",
                        "value": 60,
                    },
                    "quantitativePrecipitation": {
                        "unitCode": "wmoUnit:mm",
                        "value": 2.54,
                    },
                    "shortForecast": "Chance Showers",
                    "detailedForecast": "A chance of showers.",
                },
                {
                    "number": 2,
                    "startTime": "2026-06-16T20:00:00+00:00",
                    "temperature": 74,
                    "temperatureUnit": "F",
                    "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 65},
                    "windSpeed": "15 mph",
                    "probabilityOfPrecipitation": {
                        "unitCode": "wmoUnit:percent",
                        "value": 20,
                    },
                    "shortForecast": "Partly Sunny",
                },
            ],
        },
    }
