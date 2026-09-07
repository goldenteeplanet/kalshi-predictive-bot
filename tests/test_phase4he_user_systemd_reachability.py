from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.workstation.user_systemd_reachability import (
    UserSystemdReachabilityError,
    make_user_systemd_probe_observation,
    probe_user_systemd_reachability,
    validate_user_systemd_reachability_evidence,
)


def test_reachable_evidence_is_deterministic_redacted_and_read_only() -> None:
    observation = _observation()
    first = probe_user_systemd_reachability(observation)
    second = probe_user_systemd_reachability(observation)
    validate_user_systemd_reachability_evidence(first)
    assert first.status == "REACHABLE"
    assert first.evidence_hash == second.evidence_hash
    assert first.output_hash != "active"
    assert first.host_restart_authorized is False


def test_all_unreachable_reasons_are_explicit_and_sorted() -> None:
    evidence = probe_user_systemd_reachability(
        _observation(exit_code=1, manager=False, runtime=False, dbus=False, duration=5_001)
    )
    assert evidence.status == "UNREACHABLE"
    assert evidence.reasons == tuple(sorted(evidence.reasons))
    assert "USER_SYSTEMD_MANAGER_UNREACHABLE" in evidence.reasons
    assert "USER_RUNTIME_DIRECTORY_MISSING" in evidence.reasons
    assert evidence.alert_required is True


def test_exact_duration_and_freshness_boundaries_pass() -> None:
    exact = probe_user_systemd_reachability(_observation(duration=5_000, age=120))
    assert exact.status == "REACHABLE"
    assert probe_user_systemd_reachability(_observation(duration=5_001)).status == "UNREACHABLE"
    assert probe_user_systemd_reachability(_observation(age=121)).status == "STALE"


def test_incomplete_evidence_does_not_infer_root_cause() -> None:
    evidence = probe_user_systemd_reachability(
        _observation(complete=False, exit_code=None, manager=False, runtime=False, dbus=False)
    )
    assert evidence.status == "INCOMPLETE"
    assert evidence.reasons == ("USER_SYSTEMD_PROBE_INCOMPLETE",)
    assert evidence.recovery_authorized is False


def test_output_and_numeric_bounds_fail_closed() -> None:
    with pytest.raises(UserSystemdReachabilityError, match="PROBE_OUTPUT_BOUND_EXCEEDED"):
        _observation(output="x" * 4_097)
    with pytest.raises(UserSystemdReachabilityError, match="PROBE_BOUND_INVALID"):
        probe_user_systemd_reachability(_observation(), max_duration_milliseconds=True)
    with pytest.raises(UserSystemdReachabilityError, match="OBSERVATION_FIELD_INVALID"):
        _observation(duration=-1)


def test_observation_result_and_safety_tampering_fail_closed() -> None:
    observation = _observation()
    with pytest.raises(UserSystemdReachabilityError, match="OBSERVATION_HASH_MISMATCH"):
        probe_user_systemd_reachability(replace(observation, manager_reachable=False))
    evidence = probe_user_systemd_reachability(observation)
    with pytest.raises(UserSystemdReachabilityError, match="EVIDENCE_HASH_MISMATCH"):
        validate_user_systemd_reachability_evidence(replace(evidence, evidence_hash="0" * 64))
    with pytest.raises(UserSystemdReachabilityError, match="EVIDENCE_SAFETY_BOUNDARY_INVALID"):
        validate_user_systemd_reachability_evidence(
            replace(evidence, service_control_authorized=True)
        )


def test_probe_has_no_subprocess_control_notification_or_mutation_surface() -> None:
    names = set(probe_user_systemd_reachability.__code__.co_names)
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


def _observation(
    *,
    exit_code=0,
    manager=True,
    runtime=True,
    dbus=True,
    complete=True,
    duration=25,
    age=1,
    output="active",
):
    return make_user_systemd_probe_observation(
        observed_at_epoch_seconds=100,
        evidence_age_seconds=age,
        duration_milliseconds=duration,
        exit_code=exit_code,
        manager_reachable=manager,
        runtime_directory_present=runtime,
        dbus_session_present=dbus,
        complete=complete,
        probe_name="systemctl-user-is-system-running-v1",
        source_identity_hash="a" * 64,
        output=output,
    )
