from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import Base, WeatherFeature
from kalshi_predictor.weather.repository import get_latest_weather_features


def _feature(target: datetime, generated: datetime, value: str) -> WeatherFeature:
    return WeatherFeature(
        location_key="new_york", source="test", generated_at=generated,
        target_time=target, temperature_f=value, raw_json="{}", created_at=generated,
    )


def test_gh1w_exact_target_uses_one_bounded_query_and_latest_match() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = datetime(2026, 7, 16, 15, tzinfo=timezone.utc)
    now = datetime(2026, 7, 16, 14, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add_all([
            _feature(target - timedelta(hours=1), now, "70"),
            _feature(target, now, "80"),
            _feature(target, now + timedelta(minutes=1), "81"),
            _feature(target + timedelta(hours=1), now, "90"),
        ])
        session.commit()
        statements = []
        event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        row = get_latest_weather_features(session, "new_york", target_time=target)
        assert row is not None and row.temperature_f == "81"
        assert len(statements) == 1
        assert "LIMIT" in statements[0].upper()


def test_gh1w_never_falls_back_to_nearest_target() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = datetime(2026, 7, 16, 15, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(_feature(target + timedelta(minutes=1), target, "82"))
        session.commit()
        assert get_latest_weather_features(session, "new_york", target_time=target) is None
