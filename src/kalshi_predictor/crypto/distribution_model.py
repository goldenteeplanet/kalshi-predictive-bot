"""Independent strike/time/volatility probabilities for binary crypto markets."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DistributionInputs:
    spot: float
    volatility_per_minute: float
    horizon_minutes: float
    drift_per_minute: float = 0.0
    volatility_scale: float = 1.0


def threshold_probability(
    inputs: DistributionInputs,
    *,
    comparator: str,
    threshold: float | None = None,
    lower: float | None = None,
    upper: float | None = None,
) -> float | None:
    """Return a bounded probability without using the market price as an input."""
    if inputs.spot <= 0 or inputs.horizon_minutes <= 0:
        return None
    sigma = max(
        1e-9,
        inputs.volatility_per_minute
        * math.sqrt(inputs.horizon_minutes)
        * inputs.volatility_scale,
    )
    mean_log_return = inputs.drift_per_minute * inputs.horizon_minutes

    def below(value: float) -> float:
        if value <= 0:
            return 0.0
        z = (math.log(value / inputs.spot) - mean_log_return) / sigma
        return _normal_cdf(z)

    normalized = comparator.strip().upper()
    if normalized in {"ABOVE", "GREATER_THAN", "AT_OR_ABOVE"} and threshold is not None:
        probability = 1.0 - below(threshold)
    elif normalized in {"BELOW", "LESS_THAN", "AT_OR_BELOW"} and threshold is not None:
        probability = below(threshold)
    elif normalized == "RANGE" and lower is not None and upper is not None and upper > lower:
        probability = below(upper) - below(lower)
    else:
        return None
    return max(0.001, min(0.999, probability))


def inputs_from_features(
    features: dict[str, Any],
    *,
    horizon_minutes: float,
    volatility_scale: float = 1.0,
) -> DistributionInputs | None:
    spot = _float(features.get("price"))
    volatility = _first_float(
        features,
        "volatility_1h",
        "volatility_4h",
        "volatility_24h",
    )
    if spot is None or volatility is None or volatility <= 0 or horizon_minutes <= 0:
        return None
    return_1h = _float(features.get("return_1h")) or 0.0
    # Shrink observed drift heavily; unshrunk short-run returns extrapolate badly.
    drift_per_minute = max(-0.0005, min(0.0005, return_1h / 60.0 * 0.10))
    return DistributionInputs(
        spot=spot,
        volatility_per_minute=volatility,
        horizon_minutes=horizon_minutes,
        drift_per_minute=drift_per_minute,
        volatility_scale=volatility_scale,
    )


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _first_float(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(payload.get(key))
        if value is not None:
            return value
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
