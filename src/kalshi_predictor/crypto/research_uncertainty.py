"""Conservative calibration allowance conditional on justified independent clusters."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from kalshi_predictor.crypto.research_costs import (
    CostComponent,
    DependenceCounts,
    EvidenceStatus,
    unknown,
)


@dataclass(frozen=True)
class CalibrationCluster:
    cluster_id: str
    # Equal-cluster mean signed (outcome - forecast probability) in [-1, 1].
    mean_residual: Decimal
    available_at: datetime
    original_sha256: str


def uncertainty_allowance(
    *,
    clusters: tuple[CalibrationCluster, ...],
    counts: DependenceCounts,
    decision_at: datetime,
    minimum_independent_events: int,
    alpha: float,
    independence_review_sha256: str | None,
    segment_protocol_sha256: str | None,
) -> CostComponent:
    """Absolute mean bias plus two-sided Hoeffding radius for range [-1,1].

    This is conditional on a reviewed independence argument and a prospectively
    fixed matching segment. It is NOT a per-event probability guarantee and
    does not establish distribution stability or grant model promotion.
    Minimum N and alpha must come from the frozen cohort protocol.
    """
    method = "ABS_CLUSTER_BIAS_PLUS_HOEFFDING_RANGE_TWO_V1"
    if type(minimum_independent_events) is not int or minimum_independent_events < 2:
        raise ValueError("PREDECLARED_MINIMUM_N_REQUIRED")
    if not math.isfinite(alpha) or not 0 < alpha < 1 or decision_at.utcoffset() is None:
        raise ValueError("VALID_ALPHA_AND_AWARE_CLOCK_REQUIRED")
    if not independence_review_sha256 or not segment_protocol_sha256:
        return unknown(method, "INDEPENDENCE_AND_MATCHED_SEGMENT_REVIEW_REQUIRED")
    n = len(clusters)
    if counts.independent_events is None or counts.independent_events != n:
        return unknown(method, "INDEPENDENT_CLUSTER_COUNT_NOT_ESTABLISHED")
    if n < minimum_independent_events:
        return unknown(method, "INSUFFICIENT_INDEPENDENT_CALIBRATION_EVIDENCE")
    if counts.clusters != n or len({c.cluster_id for c in clusters}) != n:
        raise ValueError("ONE_ROW_PER_DISTINCT_INDEPENDENT_CLUSTER_REQUIRED")
    for cluster in clusters:
        if (
            not cluster.cluster_id
            or cluster.available_at.utcoffset() is None
            or cluster.available_at >= decision_at
        ):
            raise ValueError("PREDECISION_CALIBRATION_EVIDENCE_REQUIRED")
        if (
            not isinstance(cluster.mean_residual, Decimal)
            or not cluster.mean_residual.is_finite()
            or not -1 <= cluster.mean_residual <= 1
        ):
            raise ValueError("BOUNDED_CLUSTER_RESIDUAL_REQUIRED")
    bias = abs(sum((c.mean_residual for c in clusters), Decimal(0)) / n)
    radius = Decimal(str(math.sqrt(2 * math.log(2 / alpha) / n)))
    value = min(Decimal(1), bias + radius)
    evidence = (
        independence_review_sha256,
        segment_protocol_sha256,
        *(c.original_sha256 for c in clusters),
    )
    return CostComponent(
        value,
        EvidenceStatus.ESTIMATED,
        method,
        evidence,
        "CONDITIONAL_INDEPENDENT_CLUSTER_BIAS_BOUND_NOT_PROMOTION",
    )
