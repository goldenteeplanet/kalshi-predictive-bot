from pathlib import Path

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1t import run_atomic_activation


def test_gh1t_requires_verified_backup_before_session(tmp_path: Path) -> None:
    result = run_atomic_activation(
        session_factory=lambda: (_ for _ in ()).throw(AssertionError("no session")),
        settings=Settings(), verified_backup=tmp_path / "missing.gz",
    )
    assert result["reason"] == "VERIFIED_BACKUP_MISSING"
    assert result["database_writes"] == 0


def test_gh1t_exact_weather_lookup_does_not_fall_back_to_other_targets() -> None:
    from datetime import datetime, timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from kalshi_predictor.data.schema import Base, WeatherFeature
    from kalshi_predictor.phase_gh1t import _get_exact_weather_feature

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert _get_exact_weather_feature(
            session, "new_york", target_time=datetime(2026, 7, 16, 6, tzinfo=timezone.utc)
        ) is None
