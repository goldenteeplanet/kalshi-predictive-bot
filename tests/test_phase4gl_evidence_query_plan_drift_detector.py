from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.phase4cd.evidence_query_plan_drift_detector import (
    EvidenceQueryPlanDriftError,
    detect_evidence_query_plan_drift,
    make_query_plan_observation,
    validate_query_plan_drift,
)
from kalshi_predictor.phase4cd.evidence_query_regression_corpus import (
    build_evidence_query_regression_corpus,
    make_regression_case,
)


def test_matching_plans_are_stable_and_order_independent() -> None:
    corpus = _corpus()
    first = detect_evidence_query_plan_drift(
        corpus=corpus, observations=[_observation(corpus, "b"), _observation(corpus, "a")]
    )
    second = detect_evidence_query_plan_drift(
        corpus=corpus, observations=[_observation(corpus, "a"), _observation(corpus, "b")]
    )
    validate_query_plan_drift(first)
    assert first.status == "STABLE"
    assert first.observations_hash == second.observations_hash
    assert first.execution_authorized is False


def test_empty_partial_and_observation_bound_fail_closed() -> None:
    corpus = _corpus()
    with pytest.raises(EvidenceQueryPlanDriftError, match="OBSERVATIONS_EMPTY"):
        detect_evidence_query_plan_drift(corpus=corpus, observations=[])
    with pytest.raises(EvidenceQueryPlanDriftError, match="CORPUS_INPUT_INVALID"):
        detect_evidence_query_plan_drift(corpus=None, observations=[_observation(corpus, "a")])
    with pytest.raises(EvidenceQueryPlanDriftError, match="OBSERVATION_BOUND_EXCEEDED"):
        detect_evidence_query_plan_drift(
            corpus=corpus,
            observations=[_observation(corpus, "a"), _observation(corpus, "b")],
            max_observations=1,
        )


def test_exact_freshness_boundary_is_stable() -> None:
    corpus = _corpus()
    result = detect_evidence_query_plan_drift(
        corpus=corpus,
        observations=[_observation(corpus, "a", age=300)],
        max_evidence_age_seconds=300,
    )
    assert result.status == "STABLE"


def test_plan_change_is_drift_and_stale_suppresses_drift_details() -> None:
    corpus = _corpus()
    drift = detect_evidence_query_plan_drift(
        corpus=corpus, observations=[_observation(corpus, "a", current="changed")]
    )
    assert drift.status == "DRIFT"
    assert drift.drifted_query_fingerprints == ("a",)
    stale = detect_evidence_query_plan_drift(
        corpus=corpus,
        observations=[_observation(corpus, "a", current="changed", age=301)],
        max_evidence_age_seconds=300,
    )
    assert stale.status == "STALE"
    assert stale.drifted_query_fingerprints == ()


def test_malformed_duplicate_tampered_and_lineage_inputs_fail_closed() -> None:
    corpus = _corpus()
    with pytest.raises(EvidenceQueryPlanDriftError, match="OBSERVATION_FIELD_INVALID"):
        _observation(corpus, "a", age=-1)
    with pytest.raises(EvidenceQueryPlanDriftError, match="QUERY_OBSERVATION_DUPLICATE"):
        detect_evidence_query_plan_drift(
            corpus=corpus, observations=[_observation(corpus, "a"), _observation(corpus, "a")]
        )
    observation = _observation(corpus, "a")
    with pytest.raises(EvidenceQueryPlanDriftError, match="OBSERVATION_HASH_MISMATCH"):
        detect_evidence_query_plan_drift(
            corpus=corpus, observations=[replace(observation, current_plan_hash="bad")]
        )
    with pytest.raises(EvidenceQueryPlanDriftError, match="SOURCE_WATERMARK_MISMATCH"):
        detect_evidence_query_plan_drift(
            corpus=corpus, observations=[_observation(corpus, "a", watermark="other")]
        )


def test_stale_corpus_fails_closed() -> None:
    stale = build_evidence_query_regression_corpus([_case("a", age=2)], max_evidence_age_seconds=1)
    with pytest.raises(EvidenceQueryPlanDriftError, match="CORPUS_NOT_READY"):
        detect_evidence_query_plan_drift(corpus=stale, observations=[_observation(stale, "a")])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    corpus = _corpus()
    result = detect_evidence_query_plan_drift(
        corpus=corpus, observations=[_observation(corpus, "a")]
    )
    with pytest.raises(EvidenceQueryPlanDriftError, match="DETECTOR_HASH_MISMATCH"):
        validate_query_plan_drift(replace(result, detector_hash="0" * 64))
    with pytest.raises(EvidenceQueryPlanDriftError, match="DETECTOR_SAFETY_BOUNDARY_INVALID"):
        validate_query_plan_drift(replace(result, execution_authorized=True))


def test_detector_has_no_explain_query_or_mutation_surface() -> None:
    names = set(detect_evidence_query_plan_drift.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "explain", "open", "replace", "unlink"}
    )


def _case(case_id: str, *, age: int = 1):
    return make_regression_case(
        case_id=case_id,
        query_fingerprint=case_id,
        fixture_hash="fixture:" + case_id,
        expected_result_hash="result:" + case_id,
        source_identity_hash="a" * 64,
        source_watermark="w",
        evidence_age_seconds=age,
    )


def _corpus():
    return build_evidence_query_regression_corpus([_case("a"), _case("b")])


def _observation(
    corpus,
    query: str,
    *,
    current: str = "plan",
    age: int = 1,
    watermark: str = "w",
):
    return make_query_plan_observation(
        query_fingerprint=query,
        baseline_plan_hash="plan",
        current_plan_hash=current,
        corpus_hash=corpus.corpus_hash,
        source_identity_hash="a" * 64,
        source_watermark=watermark,
        evidence_age_seconds=age,
    )
