from dataclasses import FrozenInstanceError

import pytest

from kalshi_predictor.roadmap.paper_quota import (
    PaperQuotaCandidate,
    select_paper_candidates_by_category,
)


def _candidate(
    candidate_id: str,
    category: str,
    rank: int,
    **flags: bool,
) -> PaperQuotaCandidate:
    return PaperQuotaCandidate(
        candidate_id=candidate_id,
        ticker=f"T-{candidate_id}",
        category=category,
        rank=rank,
        evidence={"source": "fixture"},
        **flags,
    )


def test_selector_applies_per_category_quotas_and_preserves_rank() -> None:
    result = select_paper_candidates_by_category(
        [
            _candidate("c3", "crypto", 3),
            _candidate("w2", "weather", 2),
            _candidate("c1", "crypto", 1),
            _candidate("w1", "weather", 1),
            _candidate("c2", "crypto", 2),
        ],
        category_quotas={"crypto": 2, "weather": 1},
        cycle_id="cycle-123",
    )

    assert [row.candidate.candidate_id for row in result.selected] == ["c1", "c2", "w1"]
    assert [row.category_at_decision.rank_within_category for row in result.selected] == [1, 2, 1]
    assert [row["candidate_id"] for row in result.rejected] == ["c3", "w2"]
    assert result.funnel["rejections_by_reason"]["category_quota"] == 2
    assert result.funnel["by_category"]["crypto"]["selected"] == 2
    assert result.funnel["by_category"]["weather"]["selected"] == 1


def test_selector_rejects_ineligible_evidence_with_explicit_funnel_counts() -> None:
    result = select_paper_candidates_by_category(
        [
            _candidate("unknown", "mystery", 1),
            _candidate("synthetic", "crypto", 1, synthetic=True),
            _candidate("stale", "weather", 1, stale=True),
            _candidate("relaxed", "sports", 1, threshold_relaxed=True),
            _candidate("good", "general", 1),
        ],
        category_quotas={"crypto": 1, "weather": 1, "sports": 1, "general": 1},
        cycle_id="cycle-safe",
    )

    assert [row.candidate.candidate_id for row in result.selected] == ["good"]
    assert result.funnel["rejections_by_reason"] == {
        "unknown_category": 1,
        "synthetic": 1,
        "stale": 1,
        "threshold_relaxed": 1,
        "category_quota": 0,
    }
    assert result.funnel["by_category"]["unknown"]["unknown_category"] == 1
    assert result.funnel["by_category"]["crypto"]["synthetic"] == 1
    assert result.funnel["paper_orders_created"] == 0
    assert result.funnel["thresholds_lowered"] is False


def test_selected_category_evidence_and_payload_are_immutable_copies() -> None:
    source_evidence = {"source": "fixture"}
    candidate = PaperQuotaCandidate("c1", "BTC", "CRYPTO", 1, evidence=source_evidence)
    result = select_paper_candidates_by_category(
        [candidate], category_quotas={"crypto": 1}, cycle_id="cycle-immutable"
    )
    selected = result.selected[0]
    source_evidence["source"] = "mutated"

    assert selected.category_at_decision.category == "crypto"
    assert selected.category_at_decision.cycle_id == "cycle-immutable"
    assert selected.candidate.evidence == {"source": "fixture"}
    with pytest.raises(FrozenInstanceError):
        selected.category_at_decision.category = "weather"  # type: ignore[misc]
    with pytest.raises(TypeError):
        selected.candidate.evidence["source"] = "changed"  # type: ignore[index]


def test_equal_ranks_are_stable_and_zero_quota_rejects_all() -> None:
    result = select_paper_candidates_by_category(
        [_candidate("first", "news", 1), _candidate("second", "news", 1)],
        category_quotas={"news": 0},
        cycle_id="cycle-zero",
    )

    assert result.selected == ()
    assert [row["candidate_id"] for row in result.rejected] == ["first", "second"]
    assert result.funnel["by_category"]["news"]["category_quota"] == 2


@pytest.mark.parametrize(
    "quotas, cycle_id",
    [({"invalid": 1}, "cycle"), ({"crypto": -1}, "cycle"), ({"crypto": 1}, "")],
)
def test_invalid_selector_configuration_is_rejected(quotas: dict[str, int], cycle_id: str) -> None:
    with pytest.raises(ValueError):
        select_paper_candidates_by_category([], category_quotas=quotas, cycle_id=cycle_id)
