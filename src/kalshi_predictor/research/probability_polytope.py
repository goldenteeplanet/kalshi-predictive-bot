"""Information gates for bid/ask probability polytopes."""

from __future__ import annotations

import math
from typing import Any

MAX_MEAN_TIGHTENED_WIDTH = 0.20
MAX_COORDINATE_TIGHTENED_WIDTH = 0.75
MAX_SIMPLEX_VOLUME_RATIO_UPPER_BOUND = 0.01


def polytope_information(bounds: list[dict[str, Any]]) -> dict[str, Any]:
    lower = [max(0.0, min(1.0, float(row["lower"]))) for row in bounds]
    upper = [max(0.0, min(1.0, float(row["upper"]))) for row in bounds]
    feasible = bool(bounds) and all(lo <= hi for lo, hi in zip(lower, upper, strict=True))
    feasible = feasible and sum(lower) <= 1.0 + 1e-9 and sum(upper) >= 1.0 - 1e-9
    if not feasible:
        return _result(False, [], None)
    tightened = []
    for index in range(len(bounds)):
        other_upper = sum(upper) - upper[index]
        other_lower = sum(lower) - lower[index]
        lo = max(lower[index], 1.0 - other_upper)
        hi = min(upper[index], 1.0 - other_lower)
        tightened.append(max(0.0, hi - lo))
    volume = _simplex_volume_ratio_upper_bound(tightened)
    return _result(True, tightened, volume)


def _simplex_volume_ratio_upper_bound(widths: list[float]) -> float:
    """Conservative projected-box upper bound relative to simplex volume.

    Dropping any one coordinate maps the probability simplex to an
    (n-1)-simplex of volume 1/(n-1)!. The feasible projection is contained in
    the box formed by the remaining tightened coordinate widths. Taking the
    smallest such projected box gives a valid normalized volume upper bound.
    """
    if len(widths) <= 1:
        return 0.0
    log_factorial = math.lgamma(len(widths))
    candidates = []
    for dropped in range(len(widths)):
        retained = [width for index, width in enumerate(widths) if index != dropped]
        if any(width <= 0.0 for width in retained):
            candidates.append(float("-inf"))
        else:
            candidates.append(log_factorial + sum(math.log(width) for width in retained))
    best_log = min(candidates)
    return 0.0 if best_log == float("-inf") else min(1.0, math.exp(best_log))


def _result(
    feasible: bool, widths: list[float], volume: float | None
) -> dict[str, Any]:
    mean_width = sum(widths) / len(widths) if widths else None
    max_width = max(widths) if widths else None
    checks = {
        "simplex_feasible": feasible,
        "mean_tightened_width": mean_width is not None
        and mean_width <= MAX_MEAN_TIGHTENED_WIDTH,
        "maximum_tightened_width": max_width is not None
        and max_width <= MAX_COORDINATE_TIGHTENED_WIDTH,
        "simplex_volume_ratio_upper_bound": volume is not None
        and volume <= MAX_SIMPLEX_VOLUME_RATIO_UPPER_BOUND,
    }
    return {
        "simplex_feasible": feasible,
        "mean_tightened_width": mean_width,
        "maximum_tightened_width": max_width,
        "simplex_volume_ratio_upper_bound": volume,
        "thresholds": {
            "maximum_mean_tightened_width": MAX_MEAN_TIGHTENED_WIDTH,
            "maximum_coordinate_tightened_width": MAX_COORDINATE_TIGHTENED_WIDTH,
            "maximum_simplex_volume_ratio_upper_bound": (
                MAX_SIMPLEX_VOLUME_RATIO_UPPER_BOUND
            ),
        },
        "checks": checks,
        "gate_passed": all(checks.values()),
    }
