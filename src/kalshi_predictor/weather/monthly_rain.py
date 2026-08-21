from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import mean, pstdev

_MTD = re.compile(r"MONTH TO DATE\s+(?P<value>MM|T|\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class MonthlyRainCalibration:
    sample_count: int
    bias_inches: Decimal
    residual_sigma_inches: Decimal | None
    passed: bool
    blocker: str | None


def parse_cli_month_to_date(text: str) -> Decimal | None:
    match = _MTD.search(text.upper())
    if match is None or match.group("value") == "MM":
        return None
    if match.group("value") == "T":
        return Decimal("0.005")
    return Decimal(match.group("value"))


def calibrate_monthly_rain(
    samples: Iterable[tuple[Decimal, Decimal]], *, minimum_samples: int = 12
) -> MonthlyRainCalibration:
    residuals = [float(actual - predicted) for predicted, actual in samples]
    count = len(residuals)
    bias = Decimal(str(mean(residuals))) if residuals else Decimal("0")
    sigma = Decimal(str(pstdev(residuals))) if count >= 2 else None
    passed = count >= minimum_samples and sigma is not None and sigma > 0
    return MonthlyRainCalibration(
        sample_count=count,
        bias_inches=bias,
        residual_sigma_inches=sigma,
        passed=passed,
        blocker=None if passed else "MONTHLY_RAIN_CALIBRATION_SAMPLE_TOO_SMALL",
    )


def remaining_noaa_expected_inches(
    rows: Iterable[tuple[datetime, Decimal | None]], *, after: datetime, through: datetime
) -> tuple[Decimal | None, int]:
    values = [amount for at, amount in rows if after < at <= through and amount is not None]
    if not values:
        return None, 0
    return sum(values, Decimal("0")), len(values)


def probability_above(
    *,
    threshold_inches: Decimal,
    month_to_date_inches: Decimal,
    remaining_expected_inches: Decimal,
    calibration: MonthlyRainCalibration,
) -> Decimal | None:
    if not calibration.passed or calibration.residual_sigma_inches is None:
        return None
    location = month_to_date_inches + remaining_expected_inches + calibration.bias_inches
    sigma = float(calibration.residual_sigma_inches)
    z = float(threshold_inches - location) / sigma
    probability = 0.5 * math.erfc(z / math.sqrt(2))
    return Decimal(str(max(0.01, min(0.99, probability))))


def fit_isotonic(points: Iterable[tuple[float, int]]) -> list[dict[str, float]]:
    """Fit weighted PAV blocks for a non-decreasing probability mapping."""
    ordered = sorted((float(probability), int(outcome)) for probability, outcome in points)
    blocks: list[dict[str, float]] = []
    for probability, outcome in ordered:
        blocks.append(
            {
                "min_probability": probability,
                "max_probability": probability,
                "sum": float(outcome),
                "n": 1.0,
            }
        )
        while (
            len(blocks) >= 2
            and blocks[-2]["sum"] / blocks[-2]["n"] > blocks[-1]["sum"] / blocks[-1]["n"]
        ):
            right = blocks.pop()
            left = blocks.pop()
            blocks.append(
                {
                    "min_probability": left["min_probability"],
                    "max_probability": right["max_probability"],
                    "sum": left["sum"] + right["sum"],
                    "n": left["n"] + right["n"],
                }
            )
    return [
        {
            "min_probability": block["min_probability"],
            "max_probability": block["max_probability"],
            "calibrated_probability": block["sum"] / block["n"],
            "n": block["n"],
        }
        for block in blocks
    ]


def apply_isotonic(probability: float, blocks: list[dict[str, float]]) -> float:
    if not blocks:
        return probability
    for block in blocks:
        if probability <= block["max_probability"]:
            return max(0.001, min(0.999, block["calibrated_probability"]))
    return max(0.001, min(0.999, blocks[-1]["calibrated_probability"]))


def apply_regularized_isotonic(
    probability: float,
    blocks: list[dict[str, float]],
    *,
    sample_count: int,
    prior_weight: int = 320,
) -> float:
    isotonic = apply_isotonic(probability, blocks)
    weight = sample_count / (sample_count + prior_weight)
    return max(0.001, min(0.999, (1 - weight) * probability + weight * isotonic))
