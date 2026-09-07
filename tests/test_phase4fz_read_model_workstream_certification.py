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
    independently_review_candidate,
)
from kalshi_predictor.phase4cd.read_model_provenance_dashboard import (
    build_provenance_dashboard,
)
from kalshi_predictor.phase4cd.read_model_release_candidate import build_release_candidate
from kalshi_predictor.phase4cd.read_model_staleness import StalenessEscalation
from kalshi_predictor.phase4cd.read_model_workstream_certification import (
    WorkstreamCertificationError,
    certify_read_model_workstream,
    validate_workstream_certification,
)


def test_valid_workstream_is_certified() -> None:
    candidate, review = _evidence(age=1)
    result = certify_read_model_workstream(candidate=candidate, review=review)
    validate_workstream_certification(result)
    assert result.status == "CERTIFIED"
    assert result.reasons == ()
    assert result.execution_authorized is False


def test_empty_and_partial_inputs_fail_closed() -> None:
    candidate, review = _evidence()
    for values in ((None, review), (candidate, None), (None, None)):
        with pytest.raises(WorkstreamCertificationError, match="CERTIFICATION_INPUT_INVALID"):
            certify_read_model_workstream(candidate=values[0], review=values[1])


def test_exact_freshness_boundary_certifies() -> None:
    candidate, review = _evidence(age=300, review_max_age=900)
    result = certify_read_model_workstream(
        candidate=candidate, review=review, max_evidence_age_seconds=300
    )
    assert result.status == "CERTIFIED"


def test_one_second_over_boundary_is_not_certified() -> None:
    candidate, review = _evidence(age=301, review_max_age=900)
    result = certify_read_model_workstream(
        candidate=candidate, review=review, max_evidence_age_seconds=300
    )
    assert result.status == "NOT_CERTIFIED"
    assert result.reasons == ("CERTIFICATION_EVIDENCE_STALE",)


def test_invalid_bound_and_malformed_inputs_fail_closed() -> None:
    candidate, review = _evidence()
    with pytest.raises(WorkstreamCertificationError, match="EVIDENCE_AGE_BOUND_INVALID"):
        certify_read_model_workstream(
            candidate=candidate, review=review, max_evidence_age_seconds=-1
        )
    with pytest.raises(WorkstreamCertificationError, match="CERTIFICATION_INPUT_INVALID"):
        certify_read_model_workstream(candidate={}, review=review)


def test_tampering_and_cross_link_failure_fail_closed() -> None:
    candidate, review = _evidence()
    with pytest.raises(WorkstreamCertificationError, match="CERTIFICATION_INPUT_INVALID"):
        certify_read_model_workstream(
            candidate=candidate,
            review=replace(review, review_hash="0" * 64),
        )
    other_candidate, _ = _evidence(age=2)
    with pytest.raises(WorkstreamCertificationError, match="RELEASE_REVIEW_LINK_MISMATCH"):
        certify_read_model_workstream(candidate=other_candidate, review=review)


def test_upstream_rejection_is_not_certified() -> None:
    candidate, review = _evidence(staleness="WARNING")
    result = certify_read_model_workstream(candidate=candidate, review=review)
    assert result.status == "NOT_CERTIFIED"
    assert result.reasons == (
        "INDEPENDENT_REVIEW_NOT_APPROVED",
        "RELEASE_NOT_ACCEPTED",
    )


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    candidate, review = _evidence()
    result = certify_read_model_workstream(candidate=candidate, review=review)
    with pytest.raises(WorkstreamCertificationError, match="CERTIFICATION_HASH_MISMATCH"):
        validate_workstream_certification(replace(result, certification_hash="0" * 64))
    with pytest.raises(WorkstreamCertificationError, match="CERTIFICATION_SAFETY_BOUNDARY_INVALID"):
        validate_workstream_certification(replace(result, execution_authorized=True))


def test_certifier_has_no_production_mutation_surface() -> None:
    names = set(certify_read_model_workstream.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "replace", "unlink"})


def _evidence(*, age: int = 1, review_max_age: int = 900, staleness: str = "FRESH"):
    view = ReadModelView(
        schema_version="phase4fm-evidence-read-model-v1",
        generated_at="2026-08-27T00:00:00+00:00",
        source_watermark="paper_pnl:10",
        source_database_identity_hash="a" * 64,
        age_seconds=age,
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
        age_seconds=age,
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
        head_age_seconds=age,
    )
    stale = StalenessEscalation(
        status=staleness,  # type: ignore[arg-type]
        action="NO_ACTION" if staleness == "FRESH" else "ESCALATE",
        snapshot_age_seconds=age,
        progress_age_seconds=age,
        reason="test",
        evidence_hash="f" * 64,
    )
    dashboard = build_provenance_dashboard(
        view=view, compatibility=compatibility, chain=chain, staleness=stale
    )
    replay = compare_dashboard_replays([dashboard], [dashboard])
    candidate = build_release_candidate(dashboard=dashboard, replay=replay)
    review = independently_review_candidate(
        candidate=candidate,
        dashboard=dashboard,
        replay=replay,
        max_snapshot_age_seconds=review_max_age,
        max_progress_age_seconds=review_max_age,
    )
    return candidate, review
