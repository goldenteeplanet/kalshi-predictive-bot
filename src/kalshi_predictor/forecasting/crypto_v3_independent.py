"""Independent, uncalibrated terminal-price research models; no execution authority.

Receipt visibility is enforced as well as observation time. Neither quotes nor
settlement outcomes are inputs. Declared benchmark/rules are provenance, not
certification: a terminal-price proxy cannot reproduce an averaged benchmark.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from fractions import Fraction
from typing import Any

from kalshi_predictor.crypto.distribution_model import DistributionInputs, threshold_probability

MODEL_NAME = "crypto_v3_independent"


@dataclass(frozen=True)
class PriceObservation:
    price: float
    observed_at: datetime
    received_at: datetime
    source: str
    source_sha256: str
    symbol: str


@dataclass(frozen=True)
class CryptoTarget:
    symbol: str
    comparator: str
    observation_at: datetime
    threshold: float | None = None
    lower: float | None = None
    upper: float | None = None
    benchmark: str = "UNVERIFIED"
    rule_sha256: str | None = None


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_TIMESTAMPS_REQUIRED")


def _finite(value: float) -> bool:
    return not isinstance(value, bool) and math.isfinite(value)


def _target(target: CryptoTarget) -> None:
    _aware(target.observation_at)
    if target.symbol not in {"BTC", "ETH", "SOL", "XRP", "DOGE"}:
        raise ValueError("UNSUPPORTED_SYMBOL")
    if target.comparator in {"ABOVE", "AT_OR_ABOVE", "BELOW", "AT_OR_BELOW"}:
        values = [target.threshold]
        if target.lower is not None or target.upper is not None:
            raise ValueError("CONFLICTING_STRIKES")
    elif target.comparator in {"RANGE", "RANGE_CLOSED"}:
        values = [target.lower, target.upper]
        if target.threshold is not None:
            raise ValueError("CONFLICTING_STRIKES")
    else:
        raise ValueError("UNSUPPORTED_COMPARATOR")
    if any(value is None or not _finite(value) or value <= 0 for value in values):
        raise ValueError("INVALID_STRIKE")
    if target.comparator in {"RANGE", "RANGE_CLOSED"} and target.upper <= target.lower:  # type: ignore[operator]
        raise ValueError("INVALID_RANGE")


def forecast_independent(
    prices: Sequence[PriceObservation],
    target: CryptoTarget,
    *,
    decision_at: datetime,
    max_age_seconds: float = 300,
) -> dict[str, Any]:
    """Compare fixed models on identical visible regular-cadence price history.

    Gaussian log returns use zero drift (no fitted directional optimism).
    Student-t has fixed df=3 and matched variance, a sensitivity model rather
    than an estimated tail fit. Empirical uses nonoverlapping exact-horizon
    returns; unavailable horizons are reported, never sqrt-time resampled.
    """
    _aware(decision_at)
    _target(target)
    if not _finite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("INVALID_FRESHNESS_LIMIT")
    if not 61 <= len(prices) <= 100_001:
        raise ValueError("INSUFFICIENT_OR_UNBOUNDED_HISTORY")
    if target.observation_at <= decision_at:
        raise ValueError("TARGET_NOT_FUTURE")
    for row in prices:
        if row.symbol != target.symbol:
            raise ValueError("SOURCE_SYMBOL_MISMATCH")
        _aware(row.observed_at)
        _aware(row.received_at)
        if not _finite(row.price) or row.price <= 0:
            raise ValueError("INVALID_PRICE")
        if row.observed_at > row.received_at or row.received_at > decision_at:
            raise ValueError("FUTURE_OR_INVISIBLE_SOURCE")
        if not row.source or len(row.source_sha256) != 64:
            raise ValueError("SOURCE_PROVENANCE_REQUIRED")
        if any(ch not in "0123456789abcdef" for ch in row.source_sha256):
            raise ValueError("INVALID_SOURCE_HASH")
    if len({row.source for row in prices}) != 1:
        raise ValueError("MIXED_PRICE_BASIS")
    if (decision_at - prices[-1].observed_at).total_seconds() > max_age_seconds:
        raise ValueError("STALE_SOURCE")
    gaps = [
        (b.observed_at - a.observed_at).total_seconds()
        for a, b in zip(prices, prices[1:], strict=False)
    ]
    cadence = gaps[0]
    if cadence <= 0 or any(abs(gap - cadence) > 1e-6 for gap in gaps):
        raise ValueError("REGULAR_STRICTLY_ORDERED_HISTORY_REQUIRED")
    returns = [
        math.log(b.price) - math.log(a.price) for a, b in zip(prices, prices[1:], strict=False)
    ]
    sigma = statistics.stdev(returns)
    if not math.isfinite(sigma) or sigma <= 0:
        raise ValueError("DEGENERATE_VOLATILITY")
    horizon = (target.observation_at - prices[-1].observed_at).total_seconds()
    steps = horizon / cadence
    if steps > len(returns):
        raise ValueError("HORIZON_EXCEEDS_OBSERVED_HISTORY")
    terminal_sigma = sigma * math.sqrt(steps)
    spot = prices[-1].price

    def cdf(value: float, model: str) -> float:
        z = (math.log(value) - math.log(spot)) / terminal_sigma
        if not math.isfinite(z):
            raise ValueError("NONFINITE_DISTRIBUTION")
        if model == "gaussian_log_returns":
            return 0.5 * math.erfc(-z / math.sqrt(2))
        # t3 CDF: 1/2 + [atan(x/sqrt(3)) + x*sqrt(3)/(x*x+3)]/pi.
        # Unit-variance t3 is T/sqrt(3), hence x=z*sqrt(3).
        return 0.5 + (math.atan(z) + z / (1 + z * z)) / math.pi

    def probability(model: str) -> float:
        if target.comparator in {"RANGE", "RANGE_CLOSED"}:
            assert target.upper is not None and target.lower is not None
            return cdf(target.upper, model) - cdf(target.lower, model)
        assert target.threshold is not None
        below = cdf(target.threshold, model)
        return 1 - below if target.comparator in {"ABOVE", "AT_OR_ABOVE"} else below

    comparisons: dict[str, Any] = {
        name: {"probability": probability(name), "status": "UNCALIBRATED_RESEARCH"}
        for name in ("gaussian_log_returns", "student_t_df3")
    }
    block = round(steps)
    empirical: list[float] = []
    if block >= 1 and abs(steps - block) < 1e-8:
        # Work backward to include the latest complete block, no overlapping returns.
        empirical = [
            math.log(prices[end].price) - math.log(prices[end - block].price)
            for end in range(len(prices) - 1, block - 1, -block)
        ]

    def satisfies(value: float) -> bool:
        if target.comparator in {"RANGE", "RANGE_CLOSED"}:
            assert target.lower is not None and target.upper is not None
            if target.comparator == "RANGE_CLOSED":
                return math.log(target.lower) <= value <= math.log(target.upper)
            return math.log(target.lower) <= value < math.log(target.upper)
        assert target.threshold is not None
        if target.comparator == "ABOVE":
            return value > math.log(target.threshold)
        if target.comparator == "AT_OR_ABOVE":
            return value >= math.log(target.threshold)
        if target.comparator == "BELOW":
            return value < math.log(target.threshold)
        return value <= math.log(target.threshold)

    # Closed intervals compare exact decimal representations by cross multiplication.
    # This preserves endpoint atoms without log/subtract/add roundoff or CF rounding.
    if target.comparator == "RANGE_CLOSED" and empirical:
        assert target.lower is not None and target.upper is not None
        lower, upper, current = (Fraction(str(v)) for v in (target.lower, target.upper, spot))
        closed_hits = sum(
            lower * Fraction(str(prices[end - block].price))
            <= current * Fraction(str(prices[end].price))
            <= upper * Fraction(str(prices[end - block].price))
            for end in range(len(prices) - 1, block - 1, -block)
        )
    else:
        closed_hits = None
    comparisons["empirical_matched_horizon"] = {
        "probability": (
            (
                closed_hits
                if closed_hits is not None
                else sum(satisfies(math.log(spot) + r) for r in empirical)
            )
            / len(empirical)
            if len(empirical) >= 20
            else None
        ),
        "status": "UNCALIBRATED_RESEARCH" if len(empirical) >= 20 else "INSUFFICIENT_EXACT_BLOCKS",
        "nonoverlapping_blocks": len(empirical),
    }
    comparisons["existing_distribution_zero_drift"] = {
        "probability": threshold_probability(
            DistributionInputs(spot, sigma / math.sqrt(cadence / 60), horizon / 60),
            comparator="RANGE" if target.comparator == "RANGE_CLOSED" else target.comparator,
            threshold=target.threshold,
            lower=target.lower,
            upper=target.upper,
        ),
        "status": "BASELINE_HAS_0_001_0_999_PROBABILITY_FLOOR",
    }
    payload = {
        "prices": [asdict(row) for row in prices],
        "target": asdict(target),
        "decision_at": decision_at,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()
    return {
        "model": MODEL_NAME,
        "model_version": "2-range-closed" if target.comparator == "RANGE_CLOSED" else "1",
        "probability": probability("gaussian_log_returns"),
        "generated_at": decision_at.isoformat(),
        "target": asdict(target),
        "comparisons": comparisons,
        "input_sha256": digest,
        "source_hashes": sorted({row.source_sha256 for row in prices}),
        "sources": sorted({row.source for row in prices}),
        "observations": len(prices),
        "cadence_seconds": cadence,
        "horizon_seconds": horizon,
        "sigma_per_observation": sigma,
        "independent_of_market_quotes": True,
        "calibrated": False,
        "settlement_alignment": "TERMINAL_PRICE_PROXY_UNVERIFIED",
        "confidence": "UNVALIDATED_NO_HOLDOUT",
        "blockers": [
            "MODEL_RELEASE_REQUIRED",
            "HOLDOUT_CALIBRATION_REQUIRED",
            "SETTLEMENT_BENCHMARK_AND_PAYOFF_ALIGNMENT_UNVERIFIED",
        ],
        "paper_eligible": False,
        "execution_authority": False,
    }


def compare_execution(
    probability: float,
    *,
    yes_bid: float,
    yes_ask: float,
    yes_fee: float,
    no_fee: float,
    slippage: float,
    uncertainty: float,
) -> dict[str, Any]:
    """Per-$1 payout arithmetic; supplied costs require external certification."""
    values = [probability, yes_bid, yes_ask, yes_fee, no_fee, slippage, uncertainty]
    if any(not _finite(value) or value < 0 or value > 1 for value in values):
        raise ValueError("INVALID_PROBABILITY_PRICE_OR_COST")
    if yes_bid > yes_ask:
        raise ValueError("CROSSED_BOOK")
    midpoint = (yes_bid + yes_ask) / 2
    result: dict[str, Any] = {"market_midpoint": midpoint, "paper_eligible": False}
    for side, p, price, fee, mid in (
        ("YES", probability, yes_ask, yes_fee, midpoint),
        ("NO", 1 - probability, 1 - yes_bid, no_fee, 1 - midpoint),
    ):
        losses = {
            "execution_spread": price - mid,
            "fee": fee,
            "slippage": slippage,
            "uncertainty": uncertainty,
        }
        result[side] = {
            "probability": p,
            "executable_price": price,
            "forecast_edge": p - mid,
            "gross_edge": p - price,
            **losses,
            "net_ev": p - price - fee - slippage - uncertainty,
            "largest_cost": max(losses, key=lambda name: losses[name]),
        }
    return result
