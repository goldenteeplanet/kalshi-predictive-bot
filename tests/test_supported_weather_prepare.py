import importlib.util
from pathlib import Path
from types import SimpleNamespace

from kalshi_predictor.data.repositories import encode_json

_SCRIPT = Path(__file__).parents[1] / "scripts" / "supported_weather_prepare.py"
_SPEC = importlib.util.spec_from_file_location("supported_weather_prepare", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_supported_locations = _MODULE._supported_locations


def test_script_has_exact_ticker_fallback_when_series_column_is_missing() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")

    assert 'Market.ticker.like(f"{value}-%")' in source
    assert 'Market.event_ticker.like(f"{value}-%")' in source


def test_supported_locations_uses_latest_eligible_link_only() -> None:
    rows = [
        SimpleNamespace(
            id=3,
            ticker="A",
            location_key="new_york",
            raw_json=encode_json({"point_forecast_eligible": True}),
        ),
        SimpleNamespace(
            id=2,
            ticker="A",
            location_key="unknown",
            raw_json=encode_json({"point_forecast_eligible": False}),
        ),
        SimpleNamespace(
            id=1,
            ticker="B",
            location_key="unknown",
            raw_json=encode_json({"point_forecast_eligible": False}),
        ),
    ]

    class Session:
        def scalars(self, _statement):
            return rows

    assert _supported_locations(Session(), ["A", "B"]) == ["new_york"]
