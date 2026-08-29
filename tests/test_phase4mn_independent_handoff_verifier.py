from __future__ import annotations

from scripts.local.phase4mm_handoff_parser_fuzz import bounded_verify, fuzz_corpus
from scripts.local.phase4mn_independent_handoff_verifier import (
    _first_difference,
    audit_fuzz_consensus,
    consensus_gate,
    implementation_identity,
    independent_verify,
)
from tests.test_phase4ml_recovery_handoff_package import _package


def _independent(package=None):
    return independent_verify(
        package or _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
    )


def test_independent_verifier_passes_and_matches_primary_semantics() -> None:
    package = _package()
    primary = bounded_verify(
        package,
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
    )
    independent = _independent(package)
    assert independent["verdict"] == "PASS"
    assert _first_difference(primary, independent) is None
    assert primary["semantic_summary"] == independent["semantic_summary"]


def test_consensus_gate_is_deterministic_and_identity_bound() -> None:
    identity = implementation_identity()["combined_sha256"]
    first = consensus_gate(
        _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
        expected_implementation_identity=identity,
    )
    assert first == consensus_gate(
        _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
        expected_implementation_identity=identity,
    )
    assert first["verdict"] == "PASS"
    stale = consensus_gate(
        _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
        expected_implementation_identity="0" * 64,
    )
    assert "IMPLEMENTATION_IDENTITY_STALE" in stale["errors"]


def test_every_phase4mm_fuzz_case_reaches_refusal_consensus() -> None:
    result = audit_fuzz_consensus(
        _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
    )
    assert result["verdict"] == "PASS"
    assert result["case_count"] == len(fuzz_corpus(_package()))
    assert result["case_count"] == result["consensus_count"]


def test_disagreement_localizes_first_semantic_field() -> None:
    primary = {"verdict": "PASS", "semantic_summary": {"package_sha256": "a"}}
    independent = {"verdict": "PASS", "semantic_summary": {"package_sha256": "b"}}
    assert _first_difference(primary, independent) == "package_sha256"
    independent["verdict"] = "REFUSE"
    assert _first_difference(primary, independent) == "verdict"


def test_independent_verifier_refuses_mutation_without_exception() -> None:
    for case in fuzz_corpus(_package()):
        assert _independent(case["payload"])["verdict"] == "REFUSE"


def test_independent_verifier_has_no_write_network_runtime_or_order_capability() -> None:
    safety = _independent()["safety"]
    assert safety["independent_semantics"] is True
    assert safety["offline"] is True
    assert safety["bounded"] is True
    assert safety["read_only"] is True
    assert all(
        value is False
        for key, value in safety.items()
        if key not in {"independent_semantics", "offline", "bounded", "read_only"}
    )
