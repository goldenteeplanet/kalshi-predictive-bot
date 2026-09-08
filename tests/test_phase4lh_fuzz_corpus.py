from __future__ import annotations

import pytest

from scripts.local.phase4lh_fuzz_corpus import (
    MUTATIONS,
    generate_corpus,
    minimize_failure,
)


def test_corpus_is_deterministic_and_complete() -> None:
    first = generate_corpus(4108)
    second = generate_corpus(4108)
    assert first == second
    assert first["verdict"] == "PASS"
    assert set(first["mutation_kinds"]) == set(MUTATIONS)


def test_seed_changes_corpus_identity() -> None:
    assert generate_corpus(1)["corpus_sha256"] != generate_corpus(2)["corpus_sha256"]


def test_case_ids_and_document_hashes_are_unique() -> None:
    cases = generate_corpus(4108)["cases"]
    assert len({case["case_id"] for case in cases}) == len(cases)
    assert all(len(case["document_sha256"]) == 64 for case in cases)


def test_generation_count_is_bounded() -> None:
    with pytest.raises(ValueError, match="cover every mutation"):
        generate_corpus(1, len(MUTATIONS) - 1)
    with pytest.raises(ValueError, match="remain bounded"):
        generate_corpus(1, 257)


@pytest.mark.parametrize(
    "document",
    [b'{"a":1,"a":2}', b'{"schema":"x.v1"', b'{"owned_paths":["../escape"]}'],
)
def test_minimizer_is_deterministic_and_preserves_signature(document: bytes) -> None:
    first_document, first_proof = minimize_failure(document)
    second_document, second_proof = minimize_failure(document)
    assert (first_document, first_proof) == (second_document, second_proof)
    assert first_proof["verdict"] == "PASS"
    assert first_proof["minimized_bytes"] <= first_proof["original_bytes"]


def test_minimizer_respects_evaluation_budget() -> None:
    _, proof = minimize_failure(b'{"schema":"x.v1" trailing', max_evaluations=8)
    assert proof["evaluations"] <= 8


def test_minimizer_refuses_passing_or_wrong_signature() -> None:
    with pytest.raises(ValueError, match="reproduce"):
        minimize_failure(b'{"safe":true}')
    with pytest.raises(ValueError, match="reproduce"):
        minimize_failure(b'{"a":1,"a":2}', expected_signature="OTHER")


def test_capability_extension_has_stable_refusal_signature() -> None:
    corpus = generate_corpus(4108, len(MUTATIONS))
    case = next(case for case in corpus["cases"] if case["mutation"] == "capability_extension")
    assert case["actual_verdict"] == "REFUSE"
    assert case["failure_signature"] == "FORBIDDEN_EXTENSION_CAPABILITY"
