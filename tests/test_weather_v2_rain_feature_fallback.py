from types import SimpleNamespace
from unittest.mock import Mock

from kalshi_predictor.forecasting import weather_v2


def test_rain_uses_disclosed_latest_feature_when_terminal_is_outside_horizon(
    monkeypatch,
) -> None:
    latest = SimpleNamespace(id=42)
    repository_call = Mock(side_effect=[None, latest])
    monkeypatch.setattr(weather_v2, "get_latest_weather_features", repository_call)
    link = SimpleNamespace(target_time=object(), weather_metric="RAIN")

    feature, alignment = weather_v2._features_for_link(Mock(), "austin", link)

    assert feature is latest
    assert alignment == "LATEST_RAIN_RISK_WITHIN_NOAA_HORIZON"
    assert repository_call.call_count == 2


def test_temperature_does_not_fallback_from_missing_terminal_feature(monkeypatch) -> None:
    repository_call = Mock(return_value=None)
    monkeypatch.setattr(weather_v2, "get_latest_weather_features", repository_call)
    link = SimpleNamespace(target_time=object(), weather_metric="TEMPERATURE")

    feature, alignment = weather_v2._features_for_link(Mock(), "new_york", link)

    assert feature is None
    assert alignment == "NO_COMPATIBLE_FEATURE"
    assert repository_call.call_count == 1
