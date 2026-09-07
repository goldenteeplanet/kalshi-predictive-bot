from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.wsl_liveness_evidence import (
    WslLivenessEvidenceError,
    collect_wsl_liveness_evidence,
    make_wsl_liveness_probe_result,
    validate_wsl_liveness_evidence,
)


def test_available_evidence_is_deterministic_bounded_and_read_only() -> None:
    probe = _probe()
    first = collect_wsl_liveness_evidence(probe)
    second = collect_wsl_liveness_evidence(probe)
    validate_wsl_liveness_evidence(first)
    assert first.status == "AVAILABLE"
    assert first.evidence_hash == second.evidence_hash
    assert first.output_hash != "running"
    assert first.host_restart_authorized is False


def test_unavailable_conditions_are_explicit_and_sorted() -> None:
    evidence = collect_wsl_liveness_evidence(
        _probe(exit_code=1, available=False, running=False, duration=5_001)
    )
    assert evidence.status == "UNAVAILABLE"
    assert evidence.alert_required is True
    assert evidence.reasons == tuple(sorted(evidence.reasons))
    assert "WSL_UNAVAILABLE" in evidence.reasons
    assert "WSL_LIVENESS_PROBE_TIMEOUT" in evidence.reasons


def test_exact_duration_and_freshness_boundaries_pass() -> None:
    exact = collect_wsl_liveness_evidence(_probe(duration=5_000, age=120))
    assert exact.status == "AVAILABLE"
    assert collect_wsl_liveness_evidence(_probe(duration=5_001)).status == "UNAVAILABLE"
    assert collect_wsl_liveness_evidence(_probe(age=121)).status == "STALE"


def test_incomplete_probe_and_missing_exit_code_fail_closed() -> None:
    incomplete = collect_wsl_liveness_evidence(
        _probe(complete=False, exit_code=None, available=False, running=False)
    )
    assert incomplete.status == "INCOMPLETE"
    assert incomplete.reasons == ("WSL_LIVENESS_PROBE_INCOMPLETE",)


def test_output_and_numeric_bounds_are_enforced() -> None:
    with pytest.raises(WslLivenessEvidenceError, match="PROBE_OUTPUT_BOUND_EXCEEDED"):
        _probe(output="x" * 4_097)
    with pytest.raises(WslLivenessEvidenceError, match="EVIDENCE_BOUND_INVALID"):
        collect_wsl_liveness_evidence(_probe(), max_duration_milliseconds=True)
    with pytest.raises(WslLivenessEvidenceError, match="PROBE_FIELD_INVALID"):
        _probe(duration=-1)


def test_probe_and_evidence_tampering_fail_closed() -> None:
    probe = _probe()
    with pytest.raises(WslLivenessEvidenceError, match="PROBE_RESULT_HASH_MISMATCH"):
        collect_wsl_liveness_evidence(replace(probe, wsl_available=False))
    evidence = collect_wsl_liveness_evidence(probe)
    with pytest.raises(WslLivenessEvidenceError, match="EVIDENCE_HASH_MISMATCH"):
        validate_wsl_liveness_evidence(replace(evidence, evidence_hash="0" * 64))
    with pytest.raises(WslLivenessEvidenceError, match="EVIDENCE_SAFETY_BOUNDARY_INVALID"):
        validate_wsl_liveness_evidence(replace(evidence, recovery_authorized=True))


def test_collector_has_no_subprocess_notification_control_or_mutation_surface() -> None:
    names = set(collect_wsl_liveness_evidence.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "connect",
            "execute",
            "open",
            "restart",
            "shutdown",
            "start",
            "stop",
            "systemctl",
            "toast",
            "write",
        }
    )


def _probe(
    *,
    exit_code=0,
    available=True,
    running=True,
    complete=True,
    duration=25,
    age=1,
    output="running",
):
    return make_wsl_liveness_probe_result(
        observed_at_epoch_seconds=100,
        evidence_age_seconds=age,
        duration_milliseconds=duration,
        exit_code=exit_code,
        wsl_available=available,
        distribution_running=running,
        complete=complete,
        probe_name="wsl-status-v1",
        source_identity_hash="a" * 64,
        output=output,
    )
