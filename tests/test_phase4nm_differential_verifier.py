from __future__ import annotations

import copy
from decimal import Decimal

from scripts.local.phase4nj_reproducibility_bundle import create_bundle
from scripts.local.phase4nl_conformance_corpus import VECTOR_IDS, build_corpus
from scripts.local.phase4nm_differential_verifier import (
    differential_cross_check,
    secondary_canonicalize,
    secondary_verify_bundle,
)
from tests.test_phase4mz_adversarial_backtest import _records


def test_secondary_verifier_accepts_valid_bundle_independently() -> None:
    result = secondary_verify_bundle(create_bundle(_records(), seed=1729))
    assert result == {"verdict": "PASS", "errors": []}


def test_secondary_canonicalizer_matches_key_boundary_semantics() -> None:
    left = secondary_canonicalize(
        {"caf\u00e9": Decimal("1.2300"), "t": "2026-08-01T07:00:00-05:00"}
    )
    right = secondary_canonicalize({"cafe\u0301": "1.23", "t": "2026-08-01T12:00:00Z"})
    assert left["verdict"] == right["verdict"] == "PASS"
    assert left["canonical_sha256"] == right["canonical_sha256"]


def test_complete_golden_corpus_has_no_differential_disagreement() -> None:
    corpus = build_corpus(_records())
    result = differential_cross_check(corpus, _records())
    assert result["verdict"] == "PASS"
    assert result["disagreement_vectors"] == []
    assert result["missing_vectors"] == []
    assert result["implementation_coupling"] is False


def test_asymmetric_parser_hash_replay_and_safety_mutations_are_detected() -> None:
    corpus = build_corpus(_records())
    for vector_id in (
        "canonical-invalid-time",
        "bundle-corrupt-manifest",
        "bundle-output-mismatch",
        "bundle-unsafe-capability",
    ):
        result = differential_cross_check(
            corpus,
            _records(),
            overrides={vector_id: {"verdict": "PASS", "errors": []}},
        )
        assert result["verdict"] == "REFUSE"
        assert vector_id in result["disagreement_vectors"]


def test_missing_corpus_case_and_changed_primary_expectation_refuse() -> None:
    missing = build_corpus(_records())
    missing["vectors"].pop()
    assert "MISSING_CORPUS_CASE" in differential_cross_check(missing, _records())["errors"]
    changed = build_corpus(_records())
    changed["vectors"][0]["expected"] = {"verdict": "REFUSE", "errors": []}
    assert "DIFFERENTIAL_DISAGREEMENT" in differential_cross_check(changed, _records())["errors"]


def test_secondary_refuses_manifest_scenario_and_safety_disagreement() -> None:
    mutations = []
    for kind in ("manifest", "scenario", "safety"):
        bundle = create_bundle(_records())
        if kind == "manifest":
            bundle["manifest"]["inputs"]["sha256"] = "0" * 64
        elif kind == "scenario":
            bundle["artifacts"]["scenario_identities"].reverse()
        else:
            bundle["artifacts"]["safety"]["paper_order_creation"] = True
        mutations.append(secondary_verify_bundle(bundle))
    assert all(result["verdict"] == "REFUSE" for result in mutations)


def test_differential_proof_is_deterministic_and_has_all_vectors() -> None:
    corpus = build_corpus(_records())
    first = differential_cross_check(corpus, _records())
    assert first == differential_cross_check(copy.deepcopy(corpus), _records())
    assert len(VECTOR_IDS) == 23


def test_secondary_verifier_has_no_execution_capability() -> None:
    safety = differential_cross_check(build_corpus(_records()), _records())["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
