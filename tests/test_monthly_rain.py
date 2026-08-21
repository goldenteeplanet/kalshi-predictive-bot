from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_predictor.weather.monthly_rain import (
    apply_isotonic,
    apply_regularized_isotonic,
    calibrate_monthly_rain,
    fit_isotonic,
    parse_cli_month_to_date,
    probability_above,
    remaining_noaa_expected_inches,
)


def test_parse_cli_month_to_date_and_trace() -> None:
    assert parse_cli_month_to_date("MONTH TO DATE    0.02") == Decimal("0.02")
    assert parse_cli_month_to_date("MONTH TO DATE    T") == Decimal("0.005")
    assert parse_cli_month_to_date("MONTH TO DATE    MM") is None


def test_remaining_qpf_is_bounded_to_horizon() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    total, count = remaining_noaa_expected_inches(
        [(now - timedelta(hours=1), Decimal("9")), (now + timedelta(days=1), Decimal(".2"))],
        after=now,
        through=now + timedelta(days=2),
    )
    assert (total, count) == (Decimal("0.2"), 1)


def test_calibration_and_probability_fail_closed_until_minimum_sample() -> None:
    calibration = calibrate_monthly_rain([(Decimal("1"), Decimal("1.2"))])
    assert calibration.passed is False
    assert (
        probability_above(
            threshold_inches=Decimal("1"),
            month_to_date_inches=Decimal(".2"),
            remaining_expected_inches=Decimal(".3"),
            calibration=calibration,
        )
        is None
    )


def test_calibrated_probability_is_available_after_minimum_sample() -> None:
    samples = [(Decimal(i) / 10, Decimal(i) / 10 + Decimal(i % 3) / 10) for i in range(12)]
    calibration = calibrate_monthly_rain(samples)
    result = probability_above(
        threshold_inches=Decimal("1"),
        month_to_date_inches=Decimal(".4"),
        remaining_expected_inches=Decimal(".5"),
        calibration=calibration,
    )
    assert calibration.passed is True
    assert result is not None and Decimal("0.01") <= result <= Decimal("0.99")


def test_isotonic_pool_adjacent_violators_is_monotonic() -> None:
    blocks = fit_isotonic([(0.2, 1), (0.4, 0), (0.8, 1)])
    values = [apply_isotonic(value, blocks) for value in (0.2, 0.4, 0.8)]
    assert values == sorted(values)
    assert values[:2] == [0.5, 0.5]
    regularized = apply_regularized_isotonic(0.2, blocks, sample_count=12)
    assert 0.2 < regularized < 0.5
