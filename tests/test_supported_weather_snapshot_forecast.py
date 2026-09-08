import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "supported_weather_snapshot_forecast.py"
_SPEC = importlib.util.spec_from_file_location("supported_weather_snapshot_forecast", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_exact_tickers_are_deduplicated_and_bounded() -> None:
    payload = {"active_exact_tickers": ["WX-B", "WX-A", "WX-B", "WX-C"]}

    assert _MODULE._exact_tickers(payload, 2) == ["WX-B", "WX-A"]


def test_exact_tickers_share_the_limit_across_series() -> None:
    payload = {"active_exact_tickers": ["B-1", "B-2", "B-1", "A-1", "A-2"]}

    assert _MODULE._exact_tickers(payload, 3) == ["A-1", "B-1", "A-2"]
    assert _MODULE._exact_tickers(payload, 10) == ["A-1", "B-1", "A-2", "B-2"]
    assert _MODULE._exact_tickers(payload, 0) == []


def test_exact_tickers_requires_preparation_scope() -> None:
    with pytest.raises(ValueError, match="active_exact_tickers"):
        _MODULE._exact_tickers({}, 10)


def test_launcher_runs_exact_weather_stage_immediately_after_prepare() -> None:
    launchers = (
        _SCRIPT.parent / "local" / "kalshi-fixed-rate-refresh.sh",
        _SCRIPT.parent / "kalshi-fixed-rate-refresh.sh",
    )
    launcher = next(
        path.read_text(encoding="utf-8")
        for path in launchers
        if path.exists()
        and "run_health_stage supported_weather_snapshot_forecast"
        in path.read_text(encoding="utf-8")
    )

    prepare = launcher.index("run_health_stage supported_weather_prepare")
    exact = launcher.index("run_health_stage supported_weather_snapshot_forecast")
    coinbase = launcher.index("run_health_stage coinbase_stage")
    assert prepare < exact < coinbase
