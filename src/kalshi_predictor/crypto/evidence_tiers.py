"""Prospectively declared evidence milestones, not statistical confidence labels.

Missing evidence receives a worst-case probability haircut for diagnostics.
This policy never grants model promotion, paper admission, or execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from kalshi_predictor.crypto.research_costs import CostComponent, EvidenceStatus, number


class EvidenceTier(StrEnum):
    INSUFFICIENT = "INSUFFICIENT"
    EARLY = "EARLY"
    PRELIMINARY = "PRELIMINARY"
    USEFUL = "USEFUL"
    STRONG = "STRONG"


def _sha(value: str) -> None:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("ORIGINAL_SHA256_REQUIRED")


@dataclass(frozen=True)
class TierPolicy:
    # Thresholds must be declared in the matching prospective protocol, not
    # selected after seeing the results. They are operational milestones.
    independent_event_minima: tuple[int, int, int, int]
    required_assets: frozenset[str]
    frozen_at: datetime
    protocol_sha256: str

    def __post_init__(self) -> None:
        minima = self.independent_event_minima
        if (
            len(minima) != 4
            or any(type(n) is not int or n < 2 for n in minima)
            or any(a >= b for a, b in zip(minima, minima[1:], strict=False))
        ):
            raise ValueError("FOUR_INCREASING_PREDECLARED_MINIMA_REQUIRED")
        if not self.required_assets or any(not a for a in self.required_assets):
            raise ValueError("EXPLICIT_ASSET_SCOPE_REQUIRED")
        if self.frozen_at.utcoffset() is None:
            raise ValueError("AWARE_POLICY_CLOCK_REQUIRED")
        _sha(self.protocol_sha256)


@dataclass(frozen=True)
class ModelEvidence:
    independent_events: int | None
    assets: frozenset[str]
    prospective_only: bool
    first_forecast_at: datetime
    evidence_available_at: datetime
    independence_review_sha256: str | None
    calibration_acceptable: bool
    calibration_review_sha256: str | None
    segment_stable: bool
    segment_review_sha256: str | None
    replication_passed: bool
    replication_review_sha256: str | None


def classify_evidence(
    policy: TierPolicy, evidence: ModelEvidence, *, decision_at: datetime
) -> EvidenceTier:
    clocks = (evidence.first_forecast_at, evidence.evidence_available_at, decision_at)
    if any(clock.utcoffset() is None for clock in clocks):
        raise ValueError("AWARE_EVIDENCE_CLOCK_REQUIRED")
    if not policy.frozen_at <= evidence.first_forecast_at <= evidence.evidence_available_at:
        raise ValueError("POLICY_MUST_PRECEDE_FORECAST_AND_EVIDENCE")
    if evidence.evidence_available_at >= decision_at:
        raise ValueError("PREDECISION_EVIDENCE_REQUIRED")
    n = evidence.independent_events
    if n is not None and (type(n) is not int or n < 0):
        raise ValueError("VALID_INDEPENDENT_EVENT_COUNT_REQUIRED")
    for digest in (
        evidence.independence_review_sha256,
        evidence.calibration_review_sha256,
        evidence.segment_review_sha256,
        evidence.replication_review_sha256,
    ):
        if digest is not None:
            _sha(digest)
    if (
        n is None
        or n < policy.independent_event_minima[0]
        or evidence.prospective_only is not True
        or evidence.independence_review_sha256 is None
        or not policy.required_assets <= evidence.assets
    ):
        return EvidenceTier.INSUFFICIENT
    if (
        n < policy.independent_event_minima[1]
        or evidence.calibration_acceptable is not True
        or evidence.calibration_review_sha256 is None
    ):
        return EvidenceTier.EARLY
    if (
        n < policy.independent_event_minima[2]
        or evidence.segment_stable is not True
        or evidence.segment_review_sha256 is None
    ):
        return EvidenceTier.PRELIMINARY
    if (
        n < policy.independent_event_minima[3]
        or evidence.replication_passed is not True
        or evidence.replication_review_sha256 is None
    ):
        return EvidenceTier.USEFUL
    return EvidenceTier.STRONG


def conservative_diagnostic_allowance(
    *, probability: Decimal, evidence_sha256: str
) -> CostComponent:
    """No skill assumption: true probability's lower bound is zero.

    Deducting the model probability leaves -executable_price before other
    costs. This is a quantified diagnostic bound, NOT a calibrated estimate.
    Never use this function to replace UNKNOWN in an original frozen record.
    """
    if not 0 <= number(probability) <= 1:
        raise ValueError("UNIT_PROBABILITY_REQUIRED")
    _sha(evidence_sha256)
    return CostComponent(
        probability,
        EvidenceStatus.ESTIMATED,
        "NO_SKILL_WORST_CASE_PROBABILITY_HAIRCUT_V1",
        (evidence_sha256,),
        "DIAGNOSTIC_ONLY_NOT_CALIBRATED_OR_PROMOTION_ELIGIBLE",
    )
