"""Prospective matched challengers from one original CF sample series.

All outputs remain uncalibrated research. Distribution shape and settlement
rules are declared hypotheses; no output grants execution or paper authority.
"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from statistics import NormalDist

import numpy as np

from kalshi_predictor.crypto.distribution_model import DistributionInputs, threshold_probability


def forecast_challengers(
    *,
    timestamps_ms: tuple[int, ...],
    prices: tuple[float, ...],
    decision_ms: int,
    target_ms: int,
    lower: float,
    upper: float,
    decimal_places: int,
    include_end: bool,
    seed: int,
) -> dict:
    """Freeze all supported forecasts together, before target and settlement window.

    Gaussian and t6 approximate the arithmetic average's marginal distribution.
    Empirical forecasts use nonoverlapping historical matched horizons with a
    full closing-minute average. The existing distribution function is retained
    as a distinctly labeled terminal-price proxy, not settlement aligned.
    """
    if len(prices) != len(timestamps_ms) or not 120 <= len(prices) <= 86400:
        raise ValueError("BOUNDED_MATCHED_CF_HISTORY_REQUIRED")
    if any(type(t) is not int for t in timestamps_ms):
        raise ValueError("INTEGER_TIMESTAMPS_REQUIRED")
    if any(b - a != 1000 for a, b in zip(timestamps_ms, timestamps_ms[1:], strict=False)):
        raise ValueError("CONTIGUOUS_ONE_SECOND_HISTORY_REQUIRED")
    if (
        type(decision_ms) is not int
        or type(target_ms) is not int
        or not timestamps_ms[-1] <= decision_ms < target_ms - 60000
        or not 0 <= decision_ms - timestamps_ms[-1] <= 60000
    ):
        raise ValueError("FRESH_PREWINDOW_INPUT_REQUIRED")
    if (
        not all(math.isfinite(p) and p > 0 for p in prices)
        or not math.isfinite(lower)
        or not math.isfinite(upper)
        or not 0 < lower < upper
        or type(decimal_places) is not int
        or not 0 <= decimal_places <= 8
        or type(include_end) is not bool
        or type(seed) is not int
        or seed < 0
    ):
        raise ValueError("VALID_PRICES_AND_EXPLICIT_RULE_HYPOTHESIS_REQUIRED")
    observations = np.asarray(prices, dtype=float)
    returns = np.diff(np.log(observations))
    variance_second = float(np.var(returns, ddof=1))
    level = prices[-1]
    first = target_ms - 60000 + (1000 if include_end else 0)
    times = (np.arange(60) * 1000 + first - timestamps_ms[-1]) / 1000
    average_time_covariance = float(np.minimum.outer(times, times).mean())
    sd = level * math.sqrt(variance_second * average_time_covariance)
    quantum = Decimal(1).scaleb(-decimal_places)
    # Continuous distributions assign zero probability to exact rounding ties.
    lo = float(
        ((Decimal(str(lower)) / quantum).to_integral_value(rounding=ROUND_CEILING) - Decimal(".5"))
        * quantum
    )
    hi = float(
        ((Decimal(str(upper)) / quantum).to_integral_value(rounding=ROUND_FLOOR) + Decimal(".5"))
        * quantum
    )
    if lo >= hi or sd <= 0 or not math.isfinite(sd):
        raise ValueError("NONDEGENERATE_DECLARED_AVERAGE_REQUIRED")
    normal = NormalDist(level, sd)
    gaussian = max(0.0, min(1.0, normal.cdf(hi) - normal.cdf(lo)))
    rng = np.random.Generator(np.random.PCG64(seed))
    draws = level + sd * math.sqrt(4 / 6) * rng.standard_t(6, 65536)
    student = float(np.mean((draws >= lo) & (draws <= hi)))
    horizon_seconds = (target_ms - timestamps_ms[-1]) // 1000
    if (target_ms - timestamps_ms[-1]) % 1000:
        raise ValueError("EXACT_HISTORICAL_HORIZON_REQUIRED")
    samples = []
    for origin in range(0, len(prices) - horizon_seconds, max(1, horizon_seconds)):
        end = origin + horizon_seconds + (1 if include_end else 0)
        start = end - 60
        if start <= origin or end > len(prices):
            continue
        samples.append(level * float(observations[start:end].mean()) / prices[origin])
    empirical = None if not samples else sum(lo <= x <= hi for x in samples) / len(samples)
    proxy = threshold_probability(
        DistributionInputs(level, math.sqrt(variance_second * 60), horizon_seconds / 60),
        comparator="RANGE",
        lower=lower,
        upper=upper,
    )
    return {
        "schema": "matched-multiasset-challengers-v1",
        "status": "UNCALIBRATED_RULE_HYPOTHESIS_RESEARCH",
        "decision_ms": decision_ms,
        "target_ms": target_ms,
        "seed": seed,
        "variance_per_second": variance_second,
        "average_stddev": sd,
        "sample_count": len(prices),
        "numpy_version": np.__version__,
        "models": {
            "settlement_average_gaussian_v1": {"probability": gaussian},
            "settlement_average_student_t6_v1": {
                "probability": student,
                "draws": 65536,
                "monte_carlo_standard_error": math.sqrt(student * (1 - student) / 65536),
            },
            "empirical_matched_average_v1": {
                "probability": empirical,
                "matched_nonoverlapping_windows": len(samples),
                "independent_n": None,
            },
            "existing_distribution_terminal_proxy_v1": {
                "probability": proxy,
                "settlement_aligned": False,
            },
            "microstructure": {
                "probability": None,
                "status": "UNAVAILABLE_NO_PRETARGET_FEATURE_HISTORY",
            },
            "ofi": {"probability": None, "status": "UNAVAILABLE_NO_ORDER_FLOW_HISTORY"},
        },
        "paper_eligible": False,
        "execution_authority": False,
    }
