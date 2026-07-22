from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from kalshi_predictor.roadmap.category_contract import CATEGORY_NAMES

REJECTION_REASONS = (
    "unknown_category",
    "synthetic",
    "stale",
    "threshold_relaxed",
    "category_quota",
)


@dataclass(frozen=True)
class PaperQuotaCandidate:
    candidate_id: str
    ticker: str
    category: str
    rank: int
    synthetic: bool = False
    stale: bool = False
    threshold_relaxed: bool = False
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class CategoryAtDecisionEvidence:
    category: str
    cycle_id: str
    candidate_id: str
    ticker: str
    rank_within_category: int
    input_ordinal: int


@dataclass(frozen=True)
class SelectedPaperCandidate:
    candidate: PaperQuotaCandidate
    category_at_decision: CategoryAtDecisionEvidence


@dataclass(frozen=True)
class PaperQuotaSelection:
    selected: tuple[SelectedPaperCandidate, ...]
    rejected: tuple[Mapping[str, Any], ...]
    funnel: Mapping[str, Any]


def select_paper_candidates_by_category(
    candidates: Sequence[PaperQuotaCandidate],
    *,
    category_quotas: Mapping[str, int],
    cycle_id: str,
) -> PaperQuotaSelection:
    """Select a bounded, deterministic paper sample without creating paper orders.

    Candidate rank is ascending (rank 1 is best). Ties retain input order. Categories
    are emitted in canonical ``CATEGORY_NAMES`` order, while rank is preserved within
    each category. The returned category evidence is a frozen decision-time snapshot.
    """
    if not cycle_id.strip():
        raise ValueError("cycle_id must be non-empty")
    quotas = _validate_quotas(category_quotas)
    indexed = list(enumerate(candidates))
    by_category: dict[str, list[tuple[int, PaperQuotaCandidate]]] = {
        category: [] for category in CATEGORY_NAMES
    }
    rejected: list[Mapping[str, Any]] = []
    per_category = {
        category: _empty_category_counts() for category in (*CATEGORY_NAMES, "unknown")
    }
    reason_counts = {reason: 0 for reason in REJECTION_REASONS}

    for ordinal, candidate in indexed:
        category = candidate.category.strip().lower()
        bucket = category if category in CATEGORY_NAMES else "unknown"
        per_category[bucket]["input"] += 1
        reason = _ineligible_reason(candidate, category)
        if reason is not None:
            _record_rejection(
                rejected,
                reason_counts,
                per_category,
                candidate=candidate,
                category_bucket=bucket,
                reason=reason,
            )
            continue
        per_category[category]["eligible"] += 1
        by_category[category].append((ordinal, candidate))

    selected: list[SelectedPaperCandidate] = []
    for category in CATEGORY_NAMES:
        ranked = sorted(by_category[category], key=lambda item: (item[1].rank, item[0]))
        quota = quotas.get(category, 0)
        for rank_within_category, (ordinal, candidate) in enumerate(ranked, start=1):
            if rank_within_category > quota:
                _record_rejection(
                    rejected,
                    reason_counts,
                    per_category,
                    candidate=candidate,
                    category_bucket=category,
                    reason="category_quota",
                )
                continue
            immutable_candidate = _freeze_candidate(candidate, category=category)
            selected.append(
                SelectedPaperCandidate(
                    candidate=immutable_candidate,
                    category_at_decision=CategoryAtDecisionEvidence(
                        category=category,
                        cycle_id=cycle_id,
                        candidate_id=candidate.candidate_id,
                        ticker=candidate.ticker,
                        rank_within_category=rank_within_category,
                        input_ordinal=ordinal,
                    ),
                )
            )
            per_category[category]["selected"] += 1

    populated_categories = {
        category: MappingProxyType(dict(counts))
        for category, counts in per_category.items()
        if counts["input"] > 0 or category in quotas
    }
    funnel = MappingProxyType(
        {
            "schema_version": "paper-category-quota-v1",
            "cycle_id": cycle_id,
            "input_count": len(candidates),
            "eligible_count": sum(row["eligible"] for row in per_category.values()),
            "selected_count": len(selected),
            "rejected_count": len(rejected),
            "quotas": MappingProxyType(dict(quotas)),
            "rejections_by_reason": MappingProxyType(reason_counts),
            "by_category": MappingProxyType(populated_categories),
            "paper_orders_created": 0,
            "thresholds_lowered": False,
        }
    )
    return PaperQuotaSelection(tuple(selected), tuple(rejected), funnel)


def _validate_quotas(category_quotas: Mapping[str, int]) -> dict[str, int]:
    quotas: dict[str, int] = {}
    for raw_category, raw_quota in category_quotas.items():
        category = raw_category.strip().lower()
        if category not in CATEGORY_NAMES:
            raise ValueError(f"Unknown quota category: {raw_category}")
        if isinstance(raw_quota, bool) or not isinstance(raw_quota, int) or raw_quota < 0:
            raise ValueError(f"Quota for {category} must be a non-negative integer")
        quotas[category] = raw_quota
    return quotas


def _ineligible_reason(candidate: PaperQuotaCandidate, category: str) -> str | None:
    if category not in CATEGORY_NAMES:
        return "unknown_category"
    if candidate.synthetic:
        return "synthetic"
    if candidate.stale:
        return "stale"
    if candidate.threshold_relaxed:
        return "threshold_relaxed"
    return None


def _freeze_candidate(
    candidate: PaperQuotaCandidate, *, category: str
) -> PaperQuotaCandidate:
    return PaperQuotaCandidate(
        candidate_id=candidate.candidate_id,
        ticker=candidate.ticker,
        category=category,
        rank=candidate.rank,
        synthetic=candidate.synthetic,
        stale=candidate.stale,
        threshold_relaxed=candidate.threshold_relaxed,
        evidence=MappingProxyType(dict(candidate.evidence or {})),
    )


def _record_rejection(
    rejected: list[Mapping[str, Any]],
    reason_counts: dict[str, int],
    per_category: dict[str, dict[str, int]],
    *,
    candidate: PaperQuotaCandidate,
    category_bucket: str,
    reason: str,
) -> None:
    reason_counts[reason] += 1
    per_category[category_bucket]["rejected"] += 1
    per_category[category_bucket][reason] += 1
    rejected.append(
        MappingProxyType(
            {
                "candidate_id": candidate.candidate_id,
                "ticker": candidate.ticker,
                "category": candidate.category,
                "reason": reason,
            }
        )
    )


def _empty_category_counts() -> dict[str, int]:
    return {
        "input": 0,
        "eligible": 0,
        "selected": 0,
        "rejected": 0,
        **{reason: 0 for reason in REJECTION_REASONS},
    }
