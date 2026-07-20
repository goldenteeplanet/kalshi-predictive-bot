"""Deterministic probability helpers for temperature contracts.

The observed-extreme conditioning is adapted from the MIT-licensed
``newyorkcompute/kalshi`` weather package.  These functions are deliberately
pure: callers must establish exact station, local date, and contract matching
before supplying an observed maximum or minimum.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import erf, sqrt


@dataclass(frozen=True)
class SigmaSchedule:
    hours_0_to_6: float
    hours_6_to_12: float
    hours_12_to_24: float
    hours_24_to_48: float
    hours_48_to_72: float
    hours_72_plus: float


HIGH_TEMPERATURE_SIGMA = SigmaSchedule(1.5, 2.0, 2.5, 3.5, 5.0, 6.5)
LOW_TEMPERATURE_SIGMA = SigmaSchedule(2.0, 2.5, 3.0, 4.0, 5.5, 7.0)


def sigma_for_lead_time(lead_time_hours: float, schedule: SigmaSchedule) -> float:
    """Select forecast-error sigma without interpolating between exact bands."""
    if lead_time_hours <= 6:
        return schedule.hours_0_to_6
    if lead_time_hours <= 12:
        return schedule.hours_6_to_12
    if lead_time_hours <= 24:
        return schedule.hours_12_to_24
    if lead_time_hours <= 48:
        return schedule.hours_24_to_48
    if lead_time_hours <= 72:
        return schedule.hours_48_to_72
    return schedule.hours_72_plus


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def probability_above(forecast: float, strike: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if forecast > strike else 0.0
    return 1.0 - normal_cdf((strike - forecast) / sigma)


def probability_below(forecast: float, strike: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if forecast < strike else 0.0
    return normal_cdf((strike - forecast) / sigma)


def probability_in_range(
    forecast: float,
    range_low: float,
    range_high: float,
    sigma: float,
) -> float:
    if range_high < range_low:
        raise ValueError("range_high must be greater than or equal to range_low")
    if sigma <= 0:
        return 1.0 if range_low <= forecast <= range_high else 0.0
    return normal_cdf((range_high - forecast) / sigma) - normal_cdf(
        (range_low - forecast) / sigma
    )


def probability_above_with_observed_max(
    forecast: float,
    strike: float,
    sigma: float,
    observed_max: float,
) -> float:
    if observed_max >= strike:
        return 1.0
    return probability_above(max(forecast, observed_max), strike, sigma)


def probability_below_with_observed_max(
    forecast: float,
    strike: float,
    sigma: float,
    observed_max: float,
) -> float:
    if observed_max >= strike:
        return 0.0
    return probability_below(max(forecast, observed_max), strike, sigma)


def probability_below_with_observed_min(
    forecast: float,
    strike: float,
    sigma: float,
    observed_min: float,
) -> float:
    if observed_min <= strike:
        return 1.0
    return probability_below(min(forecast, observed_min), strike, sigma)


def probability_above_with_observed_min(
    forecast: float,
    strike: float,
    sigma: float,
    observed_min: float,
) -> float:
    if observed_min <= strike:
        return 0.0
    return probability_above(min(forecast, observed_min), strike, sigma)
