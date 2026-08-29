from __future__ import annotations

from scripts.local.phase4mm_handoff_parser_fuzz import (
    MAX_DEPTH,
    bounded_verify,
    lexical_preflight,
    minimize_failure,
    run_fuzz_audit,
)
from tests.test_phase4ml_recovery_handoff_package import _package


def test_valid_package_passes_bounded_verification_deterministically() -> None:
    first = bounded_verify(
        _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
    )
    second = bounded_verify(
        _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
    )
    assert first == second
    assert first["verdict"] == "PASS"
    assert first["resource_envelope"]["within_envelope"] is True


def test_complete_corpus_refuses_every_case_and_covers_all_surfaces() -> None:
    result = run_fuzz_audit(
        _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
    )
    assert result["verdict"] == "PASS"
    assert result["case_count"] == result["refused_count"]
    assert result["case_count"] >= 20
    assert len(result["surface_coverage"]) >= 12


def test_depth_utf8_string_integer_and_duplicate_key_bounds_refuse() -> None:
    cases = [
        b"\xff",
        ("[" * (MAX_DEPTH + 1) + "]" * (MAX_DEPTH + 1)).encode(),
        b'{"x":123456789012345678901}',
        b'{"x":1,"x":2}',
    ]
    for payload in cases:
        result = bounded_verify(
            payload,
            expected_audience="dejoia.offline.recovery-reviewer",
            evaluated_at="2026-08-29T00:00:00Z",
        )
        assert result["verdict"] == "REFUSE"
        assert result["errors"]


def test_preflight_metrics_are_deterministic_and_bounded() -> None:
    first = lexical_preflight(_package())
    assert first == lexical_preflight(_package())
    assert first["verdict"] == "PASS"
    assert first["metrics"]["processing_work_units"] == len(_package().decode())


def test_failure_minimization_preserves_class_without_mutating_input() -> None:
    payload = b'{"x":1,"x":2}'
    result = minimize_failure(
        payload,
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
    )
    assert result["failure_class"] == "DUPLICATE_JSON_KEY"
    assert result["minimized_bytes"] <= result["original_bytes"]
    assert payload == b'{"x":1,"x":2}'


def test_auditor_has_no_write_extract_network_or_order_capability() -> None:
    safety = run_fuzz_audit(
        _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
    )["safety"]
    assert safety["offline"] is True
    assert safety["bounded"] is True
    assert safety["read_only"] is True
    assert all(
        value is False
        for key, value in safety.items()
        if key not in {"offline", "bounded", "read_only"}
    )
