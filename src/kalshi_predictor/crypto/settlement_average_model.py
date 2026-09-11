"""Uncalibrated arithmetic-average forecast under an explicit diffusion assumption.

The discrete-average first two moments are exact under this model; its lognormal
CDF is a moment-matched approximation, not the exact average distribution.
No quote, outcome, order, calibration release or database is an input.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

from kalshi_predictor.crypto.cf_settlement_windows import _rounded
from kalshi_predictor.crypto.settlement_target import SettlementBenchmarkTarget, aware


@dataclass(frozen=True)
class BenchmarkProcessInputs:
    symbol: str
    index_id: str
    level: Decimal
    level_observed_at: datetime
    level_received_at: datetime
    source_original: bytes
    source_sha256: str
    basis: str
    volatility_per_sqrt_minute: float
    volatility_observed_through: datetime
    volatility_received_at: datetime
    volatility_original: bytes
    volatility_sha256: str

    def validate(self, target: SettlementBenchmarkTarget, as_of: datetime) -> None:
        aware(as_of)
        for value in (
            self.level_observed_at,
            self.level_received_at,
            self.volatility_observed_through,
            self.volatility_received_at,
        ):
            aware(value)
        if not (
            self.level_observed_at <= self.level_received_at <= as_of
            and self.volatility_observed_through <= self.volatility_received_at <= as_of
        ):
            raise ValueError("PROCESS_INPUT_VISIBILITY")
        if self.symbol != target.symbol or self.index_id != target.rules.index_id:
            raise ValueError("PROCESS_TARGET_IDENTITY")
        if self.basis not in {
            "VENUE_PROXY_UNCALIBRATED_BASIS",
            "DECLARED_CF_OBSERVATION_UNVERIFIED",
        }:
            raise ValueError("EXPLICIT_UNCERTIFIED_BASIS_REQUIRED")
        if (
            type(self.level) is not Decimal
            or not self.level.is_finite()
            or self.level <= 0
            or not math.isfinite(float(self.level))
            or float(self.level) <= 0
        ):
            raise ValueError("FINITE_POSITIVE_LEVEL_REQUIRED")
        sigma = self.volatility_per_sqrt_minute
        if type(sigma) not in (int, float) or not math.isfinite(sigma) or sigma < 0:
            raise ValueError("FINITE_NONNEGATIVE_LOG_VOLATILITY_REQUIRED")
        for raw, digest in (
            (self.source_original, self.source_sha256),
            (self.volatility_original, self.volatility_sha256),
        ):
            if type(raw) is not bytes or not 0 < len(raw) <= 3000000:
                raise ValueError("BOUNDED_PROCESS_ORIGINAL_REQUIRED")
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("PROCESS_ORIGINAL_HASH_MISMATCH")


def arithmetic_average_moments(
    level: float, sigma: float, sample_minutes: tuple[float, ...]
) -> dict:
    """E[B_t]=level; Cov(B_s,B_t)=level**2 expm1(sigma**2 min(s,t))."""
    if (
        type(level) not in (int, float)
        or not math.isfinite(level)
        or level <= 0
        or type(sigma) not in (int, float)
        or not math.isfinite(sigma)
        or sigma < 0
        or type(sample_minutes) is not tuple
        or not 1 <= len(sample_minutes) <= 60
        or any(type(t) not in (int, float) or not math.isfinite(t) or t < 0 for t in sample_minutes)
        or any(b <= a for a, b in zip(sample_minutes, sample_minutes[1:], strict=False))
    ):
        raise ValueError("BOUNDED_FINITE_DIFFUSION_INPUTS_REQUIRED")
    try:
        terms = [
            math.expm1(sigma * sigma * min(a, b)) for a in sample_minutes for b in sample_minutes
        ]
        relative_variance = math.fsum(terms) / len(sample_minutes) ** 2
        variance = level * level * relative_variance
        log_variance = math.log1p(relative_variance)
        if not all(math.isfinite(x) for x in (relative_variance, variance, log_variance)):
            raise ValueError("DIFFUSION_NUMERIC_RANGE")
    except OverflowError as exc:
        raise ValueError("DIFFUSION_NUMERIC_RANGE") from exc
    if log_variance > 0 and variance == 0:
        raise ValueError("DIFFUSION_VARIANCE_UNDERFLOW")
    if sigma > 0 and sample_minutes[-1] > 0 and log_variance == 0:
        raise ValueError("DIFFUSION_NUMERIC_UNDERFLOW")
    return dict(
        mean=level,
        variance=variance,
        relative_variance=relative_variance,
        log_variance=log_variance,
        log_location=math.log(level) - log_variance / 2,
    )


def _integer_bounds(target: SettlementBenchmarkTarget) -> tuple[int | None, int | None]:
    quantum = Decimal(1).scaleb(-target.rules.decimal_places)

    def floor(v):
        ratio = Fraction(v) / Fraction(quantum)
        return ratio.numerator // ratio.denominator

    def ceil(v):
        ratio = Fraction(v) / Fraction(quantum)
        return -(-ratio.numerator // ratio.denominator)

    if target.comparator == "ABOVE":
        return floor(target.threshold) + 1, None
    if target.comparator == "AT_OR_ABOVE":
        return ceil(target.threshold), None
    if target.comparator == "BELOW":
        return None, ceil(target.threshold) - 1
    if target.comparator == "AT_OR_BELOW":
        return None, floor(target.threshold)
    return ceil(target.lower), (
        floor(target.upper) if target.comparator == "RANGE_CLOSED" else ceil(target.upper) - 1
    )


def forecast_benchmark_average(
    target: SettlementBenchmarkTarget, process: BenchmarkProcessInputs, *, as_of: datetime
) -> dict:
    """Forecast the rounded arithmetic average; target/basis evidence remains uncertified."""
    if type(target) is not SettlementBenchmarkTarget or type(process) is not BenchmarkProcessInputs:
        raise ValueError("EXACT_TARGET_AND_PROCESS_TYPES_REQUIRED")
    target_record = target.validate(as_of=as_of)
    process.validate(target, as_of)
    times = tuple(
        (datetime.fromtimestamp(t / 1000, UTC) - process.level_observed_at).total_seconds() / 60
        for t in target.rules.closing.timestamps()
    )
    moments = arithmetic_average_moments(
        float(process.level), process.volatility_per_sqrt_minute, times
    )
    low, high = _integer_bounds(target)
    q = Decimal(1).scaleb(-target.rules.decimal_places)
    if moments["log_variance"] == 0:
        rounded = _rounded(
            Fraction(process.level), target.rules.decimal_places, target.rules.rounding
        )
        quantum_value = int(Fraction(rounded) / Fraction(q))
        probability = float(
            (low is None or quantum_value >= low) and (high is None or quantum_value <= high)
        )
    elif low is not None and high is not None and low > high:
        probability = 0.0
    else:
        mode = target.rules.rounding
        # Endpoints have zero mass only in this continuous approximation.
        # The zero-volatility atom above is evaluated with exact tie rounding.
        quantum = Fraction(1, 10**target.rules.decimal_places)
        lower_edge = (
            (
                Fraction(low)
                if mode in {"FLOOR", "DOWN"}
                else Fraction(low - 1)
                if mode == "CEILING"
                else Fraction(2 * low - 1, 2)
            )
            * quantum
            if low is not None
            else None
        )
        upper_edge = (
            (
                Fraction(high + 1)
                if mode in {"FLOOR", "DOWN"}
                else Fraction(high)
                if mode == "CEILING"
                else Fraction(2 * high + 1, 2)
            )
            * quantum
            if high is not None
            else None
        )

        def z_value(value):
            if value <= 0:
                return -math.inf
            return (
                math.log(value.numerator) - math.log(value.denominator) - moments["log_location"]
            ) / math.sqrt(moments["log_variance"])

        lower_z = -math.inf if lower_edge is None else z_value(lower_edge)
        upper_z = math.inf if upper_edge is None else z_value(upper_edge)
        # Use the small complementary tail directly instead of subtracting from1.
        if lower_z >= 0:
            probability = 0.5 * (
                math.erfc(lower_z / math.sqrt(2)) - math.erfc(upper_z / math.sqrt(2))
            )
        else:
            probability = 0.5 * (
                math.erfc(-upper_z / math.sqrt(2)) - math.erfc(-lower_z / math.sqrt(2))
            )

    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("INVALID_AVERAGE_PROBABILITY")
    return dict(
        model="crypto_settlement_average_research_v1",
        model_role="RESEARCH_CHALLENGER",
        probability=probability,
        moments=moments,
        target=target_record,
        modeled_variable="ROUNDED_CF_ARITHMETIC_WINDOW_AVERAGE",
        distribution="MOMENT_MATCHED_LOGNORMAL_APPROXIMATION",
        diffusion="ZERO_ARITHMETIC_DRIFT_GEOMETRIC_BROWNIAN_LEVEL",
        volatility_unit="LOG_RETURN_PER_SQRT_MINUTE",
        basis=process.basis,
        source_sha256=process.source_sha256,
        volatility_sha256=process.volatility_sha256,
        level_observed_at=process.level_observed_at.isoformat(),
        level_received_at=process.level_received_at.isoformat(),
        volatility_observed_through=process.volatility_observed_through.isoformat(),
        volatility_received_at=process.volatility_received_at.isoformat(),
        model_input_as_of=as_of.isoformat(),
        sample_minutes_from_observation=times,
        rounding_integer_bounds=(low, high),
        prediction_recorded_at=None,
        calibrated=False,
        settlement_alignment_certified=False,
        paper_eligible=False,
        execution_authority=False,
        blockers=[
            "BENCHMARK_BASIS_UNCALIBRATED",
            "AVERAGE_DISTRIBUTION_APPROXIMATION_NOT_VALIDATED",
            "RULE_SEMANTICS_UNCERTIFIED",
            "CALIBRATION_AND_MODEL_RELEASE_REQUIRED",
        ],
    )
