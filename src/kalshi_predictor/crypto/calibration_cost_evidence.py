"""Reviewed prospective calibration evidence, never raw-case-count confidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from kalshi_predictor.crypto.cost_evidence import CostEvidenceResult, CostEvidenceStatus
from kalshi_predictor.crypto.research_costs import DependenceCounts
from kalshi_predictor.crypto.research_uncertainty import CalibrationCluster, uncertainty_allowance


@dataclass(frozen=True)
class ReviewedCalibrationPolicy:
    """Code review pins the full original-bound dataset and independence argument.

    An entry requires a separate source/provenance audit and prospective segment
    review. The dataset digest alone is not evidence of independent sampling.
    No current cohort has such a review, so the production registry is empty.
    """

    model_version: str
    segment: str
    dataset_sha256: str
    protocol_sha256: str
    independence_review_sha256: str
    protocol_committed_at: datetime
    reviewed_at: datetime
    effective_to: datetime
    minimum_independent_events: int
    alpha: float

    @property
    def version(self) -> str:
        payload = json.dumps(asdict(self), default=str, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


REVIEWED_CALIBRATION_POLICIES: tuple[ReviewedCalibrationPolicy, ...] = ()


def verify_uncertainty_evidence(
    *, model_version: str, segment: str, decision_at: datetime,
    policy_version: str | None, dataset: bytes, protocol: bytes, independence_review: bytes,
) -> CostEvidenceResult:
    """Derive residuals from reviewed prospective forecast/outcome originals.

    All clocks and scope are independently rechecked. Every dataset row is one
    reviewed independent event; duplicate event/cluster identities are rejected.
    Candidate-provided counts, residuals, alpha, and passing labels are not inputs.
    """
    if decision_at.utcoffset() is None:
        raise ValueError("CALIBRATION_AWARE_DECISION_REQUIRED")
    originals = (dataset, protocol, independence_review)
    if any(len(raw) > 8_000_000 for raw in originals):
        raise ValueError("CALIBRATION_BOUNDED_ORIGINALS_REQUIRED")
    hashes = tuple(hashlib.sha256(raw).hexdigest() for raw in originals)
    sources = tuple(zip(
        ("reviewed-dataset", "frozen-protocol", "independence-review"), hashes, strict=True,
    ))

    def unknown(reason: str) -> CostEvidenceResult:
        return CostEvidenceResult(
            "uncertainty", None, "USD_PER_ONE_DOLLAR_PAYOUT", "REVIEWED_CLUSTER_CALIBRATION",
            "ORIGINAL_CALIBRATION_RESIDUAL_V1", sources, decision_at,
            CostEvidenceStatus.UNKNOWN, (reason,), False,
        )

    matches = [p for p in REVIEWED_CALIBRATION_POLICIES if p.version == policy_version]
    if len(matches) != 1:
        return unknown("INDEPENDENT_PROSPECTIVE_CALIBRATION_REVIEW_MISSING")
    policy = matches[0]
    if not model_version or not segment or (model_version, segment) != (
        policy.model_version, policy.segment,
    ):
        return unknown("CALIBRATION_MODEL_SEGMENT_MISMATCH")
    if hashes != (policy.dataset_sha256, policy.protocol_sha256,
                  policy.independence_review_sha256) or not all(originals):
        return unknown("CALIBRATION_REVIEWED_ORIGINAL_MISMATCH")
    if any(t.utcoffset() is None for t in (
        policy.protocol_committed_at, policy.reviewed_at, policy.effective_to,
    )) or not (
        policy.protocol_committed_at < policy.reviewed_at <= decision_at < policy.effective_to
    ):
        return unknown("CALIBRATION_REVIEW_NOT_EFFECTIVE_AT_DECISION")
    rows = json.loads(dataset)
    if not isinstance(rows, list) or not rows:
        return unknown("CALIBRATION_EVENT_ROWS_REQUIRED")
    clusters = []
    events = set()
    for row in rows:
        if (row["model_version"], row["segment"]) != (model_version, segment):
            return unknown("CALIBRATION_DATASET_SCOPE_MISMATCH")
        forecast_at = datetime.fromisoformat(row["forecast_at"])
        target_at = datetime.fromisoformat(row["target_at"])
        final_at = datetime.fromisoformat(row["final_available_at"])
        if any(t.utcoffset() is None for t in (forecast_at, target_at, final_at)) or not (
            policy.protocol_committed_at <= forecast_at < target_at <= final_at
            <= policy.reviewed_at < decision_at
        ):
            return unknown("CALIBRATION_PROSPECTIVE_OR_FINALITY_CLOCK_INVALID")
        if not row["event"] or row["event"] in events or type(row["outcome"]) is not int:
            return unknown("CALIBRATION_DISTINCT_BINARY_EVENTS_REQUIRED")
        events.add(row["event"])
        probability = Decimal(row["probability"])
        if not probability.is_finite() or not 0 <= probability <= 1 or row["outcome"] not in (0, 1):
            return unknown("CALIBRATION_BINARY_PROBABILITY_REQUIRED")
        row_hash = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        clusters.append(CalibrationCluster(
            row["cluster_id"], Decimal(row["outcome"]) - probability, final_at, row_hash,
        ))
    n = len(rows)
    value = uncertainty_allowance(
        clusters=tuple(clusters), counts=DependenceCounts(n, n, n, n, n),
        decision_at=decision_at, minimum_independent_events=policy.minimum_independent_events,
        alpha=policy.alpha, independence_review_sha256=hashes[2], segment_protocol_sha256=hashes[1],
    )
    if value.value is None:
        return unknown(value.reason)
    return CostEvidenceResult(
        "uncertainty", value.value, "USD_PER_ONE_DOLLAR_PAYOUT", value.method_version,
        policy.version, sources, policy.reviewed_at, CostEvidenceStatus.ESTIMATED_WITH_SUPPORT,
        ("CONDITIONAL_BOUND_REQUIRES_SEPARATE_MODEL_ADMISSION",), True,
    )
