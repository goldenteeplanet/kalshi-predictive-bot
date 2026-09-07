from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.phase4cd.evidence_query_regression_corpus import (
    EvidenceQueryRegressionCorpusError,
    build_evidence_query_regression_corpus,
    make_regression_case,
    validate_regression_corpus,
)


def test_valid_corpus_is_ready_and_order_independent() -> None:
    first = build_evidence_query_regression_corpus([_case("b"), _case("a")])
    second = build_evidence_query_regression_corpus([_case("a"), _case("b")])
    validate_regression_corpus(first)
    assert first.status == "READY"
    assert first.cases_hash == second.cases_hash
    assert first.execution_authorized is False


def test_empty_partial_and_case_bound_fail_closed() -> None:
    with pytest.raises(EvidenceQueryRegressionCorpusError, match="CASES_EMPTY"):
        build_evidence_query_regression_corpus([])
    with pytest.raises(EvidenceQueryRegressionCorpusError, match="CASE_TYPE_INVALID"):
        build_evidence_query_regression_corpus([{}])
    with pytest.raises(EvidenceQueryRegressionCorpusError, match="CASE_BOUND_EXCEEDED"):
        build_evidence_query_regression_corpus([_case("a"), _case("b")], max_cases=1)


def test_exact_freshness_boundary_is_ready() -> None:
    corpus = build_evidence_query_regression_corpus(
        [_case("a", age=86_400)], max_evidence_age_seconds=86_400
    )
    assert corpus.status == "READY"


def test_stale_corpus_is_explicit() -> None:
    corpus = build_evidence_query_regression_corpus(
        [_case("a", age=86_401)], max_evidence_age_seconds=86_400
    )
    assert corpus.status == "STALE"
    assert corpus.reasons == ("CORPUS_EVIDENCE_STALE",)


def test_malformed_duplicate_tampered_and_mixed_lineage_fail_closed() -> None:
    with pytest.raises(EvidenceQueryRegressionCorpusError, match="CASE_FIELD_INVALID"):
        _case("a", age=-1)
    with pytest.raises(EvidenceQueryRegressionCorpusError, match="CASE_ID_DUPLICATE"):
        build_evidence_query_regression_corpus([_case("a"), _case("a")])
    case = _case("a")
    with pytest.raises(EvidenceQueryRegressionCorpusError, match="CASE_HASH_MISMATCH"):
        build_evidence_query_regression_corpus([replace(case, expected_result_hash="tampered")])
    with pytest.raises(EvidenceQueryRegressionCorpusError, match="SOURCE_LINEAGE_MIXED"):
        build_evidence_query_regression_corpus([_case("a"), _case("b", watermark="other")])


def test_query_count_is_distinct_and_bounded() -> None:
    corpus = build_evidence_query_regression_corpus(
        [_case("a", query="q1"), _case("b", query="q1"), _case("c", query="q2")]
    )
    assert corpus.case_count == 3
    assert corpus.query_count == 2


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    corpus = build_evidence_query_regression_corpus([_case("a")])
    with pytest.raises(EvidenceQueryRegressionCorpusError, match="CORPUS_HASH_MISMATCH"):
        validate_regression_corpus(replace(corpus, corpus_hash="0" * 64))
    with pytest.raises(EvidenceQueryRegressionCorpusError, match="CORPUS_SAFETY_BOUNDARY_INVALID"):
        validate_regression_corpus(replace(corpus, execution_authorized=True))


def test_corpus_builder_has_no_query_or_mutation_surface() -> None:
    names = set(build_evidence_query_regression_corpus.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "replace", "unlink"})


def _case(
    case_id: str,
    *,
    age: int = 1,
    query: str = "settled-count-v1",
    watermark: str = "paper_pnl:204",
):
    return make_regression_case(
        case_id=case_id,
        query_fingerprint=query,
        fixture_hash="fixture:" + case_id,
        expected_result_hash="result:" + case_id,
        source_identity_hash="a" * 64,
        source_watermark=watermark,
        evidence_age_seconds=age,
    )
