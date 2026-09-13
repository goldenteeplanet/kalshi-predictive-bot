"""Research uncertainty evidence; bounds never attest model calibration.

With only binary payout support, q is in [0, 1]. Thus p - q <= p.
Reserving p is the sharp assumption-free downside bound, not a calibrated
estimate. A negative lower bound cannot establish negative true expected value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum


class UncertaintyStatus(StrEnum):
    CALIBRATED = "CALIBRATED"
    CONSERVATIVE_BOUND = "CONSERVATIVE_BOUND"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class UncertaintyEvidence:
    value: Decimal | None
    unit: str
    model: str
    segment: str
    sample_window: tuple[datetime, datetime] | None
    independent_event_count: int | None
    method: str
    version: str
    evidence_strength: str
    timestamp: datetime
    status: UncertaintyStatus
    probability_lower_bound: Decimal | None
    calibrated: bool = False
    paper_support: bool = False
    execution_authority: bool = False


def binary_support_uncertainty(
    *, selected_probability: Decimal, model: str, segment: str, assessed_at: datetime,
) -> UncertaintyEvidence:
    """No data fitted, no independent N claimed, no thresholds selected.

This research fallback deliberately accepts no supplied reserve or confidence.
The only known fact is binary payout support. Source/model/rule freshness and
calibrated production uncertainty continue through their existing validators.
"""
    if (
        not isinstance(selected_probability, Decimal)
        or not selected_probability.is_finite()
        or not 0 <= selected_probability <= 1
    ):
        raise ValueError("FINITE_SELECTED_PROBABILITY_REQUIRED")
    if not model.strip() or not segment.strip() or assessed_at.utcoffset() is None:
        raise ValueError("MODEL_SEGMENT_AWARE_ASSESSMENT_REQUIRED")
    return UncertaintyEvidence(
        selected_probability, "USD_PER_ONE_DOLLAR_PAYOUT", model, segment,
        None, None, "SHARP_BINARY_SUPPORT_DOWNSIDE", "BINARY_SUPPORT_V1",
        "MATHEMATICAL_SUPPORT_ONLY_NO_EMPIRICAL_CALIBRATION", assessed_at,
        UncertaintyStatus.CONSERVATIVE_BOUND, Decimal(0),
    )


def binary_support_net_bounds(
    *, selected_probability: Decimal, executable_price: Decimal,
    fee: Decimal, snapshot_impact: Decimal, model: str, segment: str,
    assessed_at: datetime,
) -> tuple[UncertaintyEvidence, Decimal, Decimal]:
    """Bounds on EV conditional on the supplied one-contract modeled costs.

Returns (uncertainty evidence, lower bound, upper bound). Not a substitute for
cost-original replay, source validation, rule certification or admission.
"""
    evidence = binary_support_uncertainty(
        selected_probability=selected_probability, model=model,
        segment=segment, assessed_at=assessed_at,
    )
    values = (executable_price, fee, snapshot_impact)
    if any(not isinstance(v, Decimal) or not v.is_finite() for v in values):
        raise ValueError("FINITE_DECIMAL_COSTS_REQUIRED")
    if not 0 < executable_price < 1 or fee < 0 or snapshot_impact < 0:
        raise ValueError("NONNEGATIVE_COSTS_AND_INTERIOR_PRICE_REQUIRED")
    with localcontext() as ctx:
        ctx.prec = max(50, sum(len(v.as_tuple().digits) for v in values) + 10)
        lower = -executable_price - fee - snapshot_impact
        return evidence, lower, Decimal(1) + lower
