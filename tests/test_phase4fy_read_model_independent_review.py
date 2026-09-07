from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.phase4cd.read_model_chain import ReadModelChainResult
from kalshi_predictor.phase4cd.read_model_compatibility import CompatibilityResult
from kalshi_predictor.phase4cd.read_model_consumer import ReadModelView
from kalshi_predictor.phase4cd.read_model_differential_replay import (
    compare_dashboard_replays,
)
from kalshi_predictor.phase4cd.read_model_independent_review import (
    IndependentReviewError,
    independently_review_candidate,
    validate_independent_review,
)
from kalshi_predictor.phase4cd.read_model_provenance_dashboard import (
    build_provenance_dashboard,
)
from kalshi_predictor.phase4cd.read_model_release_candidate import build_release_candidate
from kalshi_predictor.phase4cd.read_model_staleness import StalenessEscalation


def test_valid_candidate_is_independently_approved() -> None:
    candidate, dashboard, replay = _evidence()
    review = independently_review_candidate(candidate=candidate, dashboard=dashboard, replay=replay)
    validate_independent_review(review)
    assert review.decision == "APPROVE"
    assert review.reasons == ()
    assert review.execution_authorized is False


def test_empty_and_partial_inputs_fail_closed() -> None:
    candidate, dashboard, replay = _evidence()
    for values in (
        (None, dashboard, replay),
        (candidate, None, replay),
        (candidate, dashboard, None),
    ):
        with pytest.raises(IndependentReviewError, match="REVIEW_INPUT_INVALID"):
            independently_review_candidate(
                candidate=values[0], dashboard=values[1], replay=values[2]
            )


def test_exact_freshness_boundaries_approve() -> None:
    candidate, dashboard, replay = _evidence(snapshot_age=300, progress_age=900)
    review = independently_review_candidate(
        candidate=candidate,
        dashboard=dashboard,
        replay=replay,
        max_snapshot_age_seconds=300,
        max_progress_age_seconds=900,
    )
    assert review.decision == "APPROVE"


@pytest.mark.parametrize(
    ("snapshot_age", "progress_age", "reason"),
    [(301, 900, "SNAPSHOT_STALE"), (300, 901, "PROGRESS_STALE")],
)
def test_over_boundary_staleness_rejects(snapshot_age: int, progress_age: int, reason: str) -> None:
    candidate, dashboard, replay = _evidence(snapshot_age=snapshot_age, progress_age=progress_age)
    review = independently_review_candidate(
        candidate=candidate,
        dashboard=dashboard,
        replay=replay,
        max_snapshot_age_seconds=300,
        max_progress_age_seconds=900,
    )
    assert review.decision == "REJECT"
    assert reason in review.reasons


def test_invalid_bounds_and_malformed_age_fail_closed() -> None:
    candidate, dashboard, replay = _evidence()
    with pytest.raises(IndependentReviewError, match="FRESHNESS_BOUND_INVALID"):
        independently_review_candidate(
            candidate=candidate,
            dashboard=dashboard,
            replay=replay,
            max_snapshot_age_seconds=-1,
        )
    malformed = dict(dashboard)
    malformed["freshness"] = dict(dashboard["freshness"], snapshot_age_seconds="1")
    with pytest.raises(IndependentReviewError, match="REVIEW_INPUT_INVALID"):
        independently_review_candidate(candidate=candidate, dashboard=malformed, replay=replay)


def test_tampering_and_cross_artifact_mismatch_fail_closed() -> None:
    candidate, dashboard, replay = _evidence()
    with pytest.raises(IndependentReviewError, match="REVIEW_INPUT_INVALID"):
        independently_review_candidate(
            candidate=replace(candidate, release_hash="0" * 64),
            dashboard=dashboard,
            replay=replay,
        )
    other_candidate, _, _ = _evidence(snapshot_age=2)
    with pytest.raises(IndependentReviewError, match="RELEASE_RECOMPUTATION_MISMATCH"):
        independently_review_candidate(
            candidate=other_candidate, dashboard=dashboard, replay=replay
        )


def test_review_result_tampering_and_safety_boundary_fail_closed() -> None:
    candidate, dashboard, replay = _evidence()
    review = independently_review_candidate(candidate=candidate, dashboard=dashboard, replay=replay)
    with pytest.raises(IndependentReviewError, match="REVIEW_HASH_MISMATCH"):
        validate_independent_review(replace(review, review_hash="0" * 64))
    with pytest.raises(IndependentReviewError, match="REVIEW_SAFETY_BOUNDARY_INVALID"):
        validate_independent_review(replace(review, execution_authorized=True))


def test_review_has_no_production_mutation_surface() -> None:
    names = set(independently_review_candidate.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "replace", "unlink"})


def _evidence(*, snapshot_age: int = 1, progress_age: int = 1):
    view = ReadModelView(
        schema_version="phase4fm-evidence-read-model-v1",
        generated_at="2026-08-27T00:00:00+00:00",
        source_watermark="paper_pnl:10",
        source_database_identity_hash="a" * 64,
        age_seconds=snapshot_age,
        guarded_paper_settled=203,
        realized_pnl="0",
        paper_order_count=204,
        evidence_lane={"evaluated": 1},
        payload_hash="b" * 64,
        manifest_hash="c" * 64,
    )
    compatibility = CompatibilityResult(
        producer_schema=view.schema_version,
        consumer_schema=view.schema_version,
        decision="COMPATIBLE",
        reason="test",
        matrix_hash="e" * 64,
        age_seconds=snapshot_age,
    )
    chain = ReadModelChainResult(
        node_count=2,
        genesis_hash="1" * 64,
        head_hash="d" * 64,
        source="paper_pnl",
        source_identity_hash="a" * 64,
        first_sequence=9,
        last_sequence=10,
        progress=1,
        head_age_seconds=snapshot_age,
    )
    staleness = StalenessEscalation(
        status="FRESH",
        action="NO_ACTION",
        snapshot_age_seconds=snapshot_age,
        progress_age_seconds=progress_age,
        reason="test",
        evidence_hash="f" * 64,
    )
    dashboard = build_provenance_dashboard(
        view=view,
        compatibility=compatibility,
        chain=chain,
        staleness=staleness,
    )
    replay = compare_dashboard_replays([dashboard], [dashboard])
    return build_release_candidate(dashboard=dashboard, replay=replay), dashboard, replay
