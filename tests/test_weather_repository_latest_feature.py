from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import Base, WeatherFeature
from kalshi_predictor.weather.repository import get_latest_weather_features


def test_latest_feature_prefers_latest_target_in_same_build() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    generated_at = datetime(2026, 8, 20, 13, tzinfo=UTC)
    older = WeatherFeature(
        location_key="austin",
        source="noaa",
        target_time=datetime(2026, 8, 20, 15, tzinfo=UTC),
        generated_at=generated_at,
        raw_json="{}",
        created_at=generated_at,
    )
    newer = WeatherFeature(
        location_key="austin",
        source="noaa",
        target_time=datetime(2026, 8, 26, 15, tzinfo=UTC),
        generated_at=generated_at,
        raw_json="{}",
        created_at=generated_at,
    )
    with Session(engine) as session:
        session.add_all((older, newer))
        session.flush()

        assert get_latest_weather_features(session, "austin") is newer
    engine.dispose()
