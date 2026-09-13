import hashlib
import math
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest
from test_settlement_target_research_router import NOW, target

from kalshi_predictor.crypto.settlement_average_model import (
    BenchmarkProcessInputs,
    arithmetic_average_moments,
    forecast_benchmark_average,
)


def process(sigma=0.01, level="100"):
    raw = b"Synthetic independently supplied process evidence, not CF authentication"
    h = hashlib.sha256(raw).hexdigest()
    return BenchmarkProcessInputs(
        "BTC",
        "BRTI",
        Decimal(level),
        NOW,
        NOW,
        raw,
        h,
        "VENUE_PROXY_UNCALIBRATED_BASIS",
        sigma,
        NOW,
        NOW,
        raw,
        h,
    )


def test_one_sample_is_exact_diffusion_terminal_moments():
    m = arithmetic_average_moments(100, 0.2, (3.0,))
    assert m["mean"] == 100
    assert m["variance"] == pytest.approx(10000 * math.expm1(0.04 * 3))
    assert m["log_variance"] == pytest.approx(0.04 * 3)


def test_two_times_independently_expanded_covariance():
    m = arithmetic_average_moments(10, 0.3, (1.0, 2.0))
    # Four matrix entries: three with min-time1, one with min-time2.
    expected = 100 * (3 * math.expm1(0.09) + math.expm1(0.18)) / 4
    assert m["variance"] == pytest.approx(expected)


def test_sixty_times_covariance_matches_independent_ordered_weights():
    times = tuple(4 + i / 60 for i in range(60))
    sigma = 0.01
    m = arithmetic_average_moments(100, sigma, times)
    weighted = (
        sum((2 * (60 - i) - 1) * math.expm1(sigma**2 * t) for i, t in enumerate(times)) / 3600
    )
    assert m["relative_variance"] == pytest.approx(weighted, rel=1e-14)
    assert (
        math.expm1(sigma**2 * times[0]) < m["relative_variance"] < math.expm1(sigma**2 * times[-1])
    )
    later = arithmetic_average_moments(100, sigma, tuple(t + 10 for t in times))
    assert later["variance"] > m["variance"]


@pytest.mark.parametrize(
    "mode,level,expected",
    [
        ("HALF_UP", "100.005", 1),
        ("HALF_EVEN", "100.005", 0),
        ("FLOOR", "100.009", 0),
        ("CEILING", "100.001", 1),
        ("DOWN", "100.009", 0),
    ],
)
def test_zero_vol_exact_rounding_atom_not_continuous_tie_assumption(mode, level, expected):
    t = target()
    t = replace(t, rules=replace(t.rules, rounding=mode))
    result = forecast_benchmark_average(t, process(0, level), as_of=NOW)
    assert result["probability"] == expected
    assert result["moments"]["variance"] == 0


def test_closed_vs_halfopen_range_atom_and_exact_decimal_context():
    t = replace(
        target(),
        comparator="RANGE_CLOSED",
        threshold=None,
        lower=Decimal("99"),
        upper=Decimal("100"),
    )
    with localcontext() as ctx:
        ctx.prec = 3
        assert forecast_benchmark_average(t, process(0), as_of=NOW)["probability"] == 1
        assert (
            forecast_benchmark_average(replace(t, comparator="RANGE"), process(0), as_of=NOW)[
                "probability"
            ]
            == 0
        )


def test_actual_average_prediction_is_not_clamped_to_market_quotes():
    t = replace(target(), threshold=Decimal("150"))
    r = forecast_benchmark_average(t, process(), as_of=NOW)
    assert 0 <= r["probability"] < 0.001
    assert r["modeled_variable"] == "ROUNDED_CF_ARITHMETIC_WINDOW_AVERAGE"
    assert r["distribution"] == "MOMENT_MATCHED_LOGNORMAL_APPROXIMATION"
    assert r["basis"] == "VENUE_PROXY_UNCALIBRATED_BASIS"
    assert not r["settlement_alignment_certified"] and not r["paper_eligible"]
    assert r["prediction_recorded_at"] is None


def test_level_observation_clock_controls_process_times_not_receipt_clock():
    p = replace(process(), level_observed_at=NOW - timedelta(seconds=30))
    r = forecast_benchmark_average(target(), p, as_of=NOW)
    assert r["sample_minutes_from_observation"][0] == 4.5
    assert r["level_observed_at"] == p.level_observed_at.isoformat()


@pytest.mark.parametrize(
    "change",
    [
        lambda p: replace(p, level_received_at=NOW + timedelta(seconds=1)),
        lambda p: replace(p, volatility_received_at=NOW + timedelta(seconds=1)),
        lambda p: replace(p, volatility_per_sqrt_minute=True),
        lambda p: replace(p, volatility_per_sqrt_minute=float("nan")),
        lambda p: replace(p, volatility_per_sqrt_minute=1e100),
        lambda p: replace(p, level=Decimal("Infinity")),
        lambda p: replace(p, index_id="SOLUSD_RTI"),
        lambda p: replace(p, source_original=b"changed"),
        lambda p: replace(p, basis="CF_ALIGNED_CERTIFIED"),
    ],
)
def test_invalid_process_never_yields_prediction(change):
    with pytest.raises(ValueError):
        forecast_benchmark_average(target(), change(process()), as_of=NOW)


def test_continuous_rounding_preimage_is_independent_of_decimal_context():
    t = replace(target(), threshold=Decimal("100.000000000000000001"))
    expected = forecast_benchmark_average(t, process(), as_of=NOW)["probability"]
    with localcontext() as ctx:
        ctx.prec = 3
        assert forecast_benchmark_average(t, process(), as_of=NOW)["probability"] == expected


def test_extreme_right_tail_and_narrow_range_preserve_representable_mass():
    t = target()
    t = replace(t, rules=replace(t.rules, decimal_places=18))
    m = arithmetic_average_moments(100, 0.01, tuple(4 + i / 60 for i in range(60)))
    lower = Decimal(str(math.exp(m["log_location"] + 9 * math.sqrt(m["log_variance"]))))
    upper = Decimal(str(math.exp(m["log_location"] + 9.01 * math.sqrt(m["log_variance"]))))
    right = forecast_benchmark_average(replace(t, threshold=lower), process(), as_of=NOW)
    assert 0 < right["probability"] < 1e-17
    assert right["probability"] == pytest.approx(
        0.5 * math.erfc(9 / math.sqrt(2)), rel=1e-10, abs=0
    )
    interval = replace(t, comparator="RANGE", threshold=None, lower=lower, upper=upper)
    mass = forecast_benchmark_average(interval, process(), as_of=NOW)["probability"]
    expected = 0.5 * (math.erfc(9 / math.sqrt(2)) - math.erfc(9.01 / math.sqrt(2)))
    assert mass > 0 and mass == pytest.approx(expected, rel=1e-9, abs=0)


def test_underflowed_level_variance_cannot_claim_exact_moments():
    with pytest.raises(ValueError, match="VARIANCE_UNDERFLOW"):
        arithmetic_average_moments(1e-200, 0.1, (1.0,))


@pytest.mark.parametrize("threshold, expected", [("1e1000", 0.0), ("1e-1000", 1.0)])
def test_finite_extreme_strikes_do_not_overflow_float_conversion(threshold, expected):
    t = replace(
        target(), threshold=Decimal(threshold), rules=replace(target().rules, rounding="FLOOR")
    )
    probability = forecast_benchmark_average(t, process(), as_of=NOW)["probability"]
    assert probability == expected
