from __future__ import annotations

import copy

from scripts.local.phase4nl_conformance_corpus import (
    REQUIRED_REFUSALS,
    VECTOR_IDS,
    build_corpus,
    run_conformance,
)
from tests.test_phase4mz_adversarial_backtest import _records


def _corpus():
    return build_corpus(_records())


def test_corpus_is_deterministic_complete_versioned_and_provenanced() -> None:
    first = _corpus()
    assert first == _corpus()
    assert len(first["vectors"]) == len(VECTOR_IDS) == 23
    assert all(row["provenance"] for row in first["vectors"])
    assert len({row["id"] for row in first["vectors"]}) == len(VECTOR_IDS)


def test_real_conformance_runner_passes_complete_golden_corpus() -> None:
    result = run_conformance(_corpus(), _records())
    assert result["verdict"] == "PASS"
    assert result["missing_refusal_classes"] == []
    assert len(REQUIRED_REFUSALS) == 22


def test_missing_and_duplicate_vectors_refuse() -> None:
    missing = _corpus()
    missing["vectors"].pop()
    assert "REQUIRED_VECTOR_MISSING" in run_conformance(missing, _records())["errors"]
    duplicate = _corpus()
    duplicate["vectors"][-1] = copy.deepcopy(duplicate["vectors"][0])
    errors = run_conformance(duplicate, _records())["errors"]
    assert "DUPLICATE_VECTOR_ID" in errors
    assert "REQUIRED_VECTOR_MISSING" in errors


def test_vector_expected_result_and_hash_drift_refuse() -> None:
    expected = _corpus()
    expected["vectors"][0]["expected"]["verdict"] = "REFUSE"
    errors = run_conformance(expected, _records())["errors"]
    assert "VECTOR_HASH_MISMATCH" in errors

    conformance = _corpus()
    row = conformance["vectors"][0]
    row["expected"]["verdict"] = "REFUSE"
    body = {key: value for key, value in row.items() if key != "vector_sha256"}
    import hashlib
    import json

    row["vector_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    assert "VERIFIER_CONFORMANCE_DRIFT" in run_conformance(conformance, _records())["errors"]


def test_manifest_corpus_version_and_envelope_hash_drift_refuse() -> None:
    version = _corpus()
    version["corpus_version"] = "future"
    errors = run_conformance(version, _records())["errors"]
    assert "CORPUS_VERSION_DRIFT" in errors
    assert "CORPUS_MANIFEST_MISMATCH" in errors
    assert "CORPUS_HASH_MISMATCH" in errors


def test_incomplete_refusal_coverage_is_detected() -> None:
    corpus = _corpus()
    corpus["vectors"] = [row for row in corpus["vectors"] if row["id"] == "valid-bundle"]
    result = run_conformance(corpus, _records())
    assert "REFUSAL_COVERAGE_INCOMPLETE" in result["errors"]
    assert result["missing_refusal_classes"]


def test_corpus_runner_has_no_execution_capability() -> None:
    safety = run_conformance(_corpus(), _records())["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
