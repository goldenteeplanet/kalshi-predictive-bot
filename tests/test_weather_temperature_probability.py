import pytest

from kalshi_predictor.weather.temperature_probability import (
    HIGH_TEMPERATURE_SIGMA,
    LOW_TEMPERATURE_SIGMA,
    probability_above,
    probability_above_with_observed_max,
    probability_above_with_observed_min,
    probability_below_with_observed_max,
    probability_below_with_observed_min,
    probability_in_range,
    sigma_for_lead_time,
)


@pytest.mark.parametrize(
    ("lead_time", "expected"),
    [(0, 1.5), (6, 1.5), (6.01, 2.0), (12, 2.0), (24, 2.5), (48, 3.5),
     (72, 5.0), (72.01, 6.5)],
)
def test_high_sigma_uses_exact_lead_time_bands(lead_time: float, expected: float) -> None:
    assert sigma_for_lead_time(lead_time, HIGH_TEMPERATURE_SIGMA) == expected


def test_low_temperature_schedule_is_more_conservative() -> None:
    assert sigma_for_lead_time(18, LOW_TEMPERATURE_SIGMA) == 3.0
    assert sigma_for_lead_time(18, HIGH_TEMPERATURE_SIGMA) == 2.5


def test_probability_helpers_cover_threshold_and_range_contracts() -> None:
    assert probability_above(80, 80, 2) == pytest.approx(0.5)
    assert probability_in_range(80, 79, 81, 2) == pytest.approx(0.3829249225)


def test_observed_max_locks_high_threshold_outcomes() -> None:
    assert probability_above_with_observed_max(80, 85, 2, 85) == 1.0
    assert probability_below_with_observed_max(80, 85, 2, 85) == 0.0


def test_observed_max_never_reduces_high_forecast_mean() -> None:
    baseline = probability_above(80, 85, 2)
    conditioned = probability_above_with_observed_max(80, 85, 2, 83)
    assert conditioned > baseline


def test_observed_min_locks_low_threshold_outcomes() -> None:
    assert probability_below_with_observed_min(40, 35, 2, 35) == 1.0
    assert probability_above_with_observed_min(40, 35, 2, 35) == 0.0


def test_observed_min_never_raises_low_forecast_mean() -> None:
    baseline = probability_below_with_observed_min(40, 35, 2, 42)
    conditioned = probability_below_with_observed_min(40, 35, 2, 37)
    assert conditioned > baseline


def test_invalid_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="range_high"):
        probability_in_range(80, 81, 79, 2)
