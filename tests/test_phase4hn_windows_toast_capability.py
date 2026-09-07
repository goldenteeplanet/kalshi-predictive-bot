from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.windows_toast_capability import (
    WindowsToastCapabilityError,
    make_windows_toast_capability_observation,
    probe_windows_toast_capability,
    validate_windows_toast_capability_evidence,
)


def test_available_capability_is_deterministic_redacted_and_does_not_notify() -> None:
    observation = _observation()
    first = probe_windows_toast_capability(observation)
    second = probe_windows_toast_capability(observation)
    validate_windows_toast_capability_evidence(first)
    assert first.status == "AVAILABLE"
    assert first.evidence_hash == second.evidence_hash
    assert first.output_hash != "available"
    assert first.notification_sent is False
    assert first.alert_delivery_authorized is False


def test_all_unavailable_reasons_are_explicit_and_sorted() -> None:
    evidence = probe_windows_toast_capability(
        _observation(
            platform="Linux",
            interactive=False,
            enabled=False,
            app_id=False,
            api=False,
            duration=5_001,
        )
    )
    assert evidence.status == "UNAVAILABLE"
    assert evidence.reasons == tuple(sorted(evidence.reasons))
    assert "INTERACTIVE_SESSION_MISSING" in evidence.reasons
    assert "WINDOWS_TOAST_API_UNAVAILABLE" in evidence.reasons


def test_exact_duration_and_freshness_boundaries_pass() -> None:
    exact = probe_windows_toast_capability(_observation(duration=5_000, age=300))
    assert exact.status == "AVAILABLE"
    assert probe_windows_toast_capability(_observation(duration=5_001)).status == "UNAVAILABLE"
    assert probe_windows_toast_capability(_observation(age=301)).status == "STALE"


def test_incomplete_probe_does_not_infer_capability() -> None:
    evidence = probe_windows_toast_capability(
        _observation(complete=False, interactive=False, enabled=False, app_id=False, api=False)
    )
    assert evidence.status == "INCOMPLETE"
    assert evidence.reasons == ("WINDOWS_TOAST_CAPABILITY_PROBE_INCOMPLETE",)
    assert evidence.host_restart_authorized is False


def test_output_and_numeric_bounds_fail_closed() -> None:
    with pytest.raises(WindowsToastCapabilityError, match="PROBE_OUTPUT_BOUND_EXCEEDED"):
        _observation(output="x" * 4_097)
    with pytest.raises(WindowsToastCapabilityError, match="PROBE_BOUND_INVALID"):
        probe_windows_toast_capability(_observation(), max_duration_milliseconds=True)
    with pytest.raises(WindowsToastCapabilityError, match="OBSERVATION_FIELD_INVALID"):
        _observation(duration=-1)


def test_observation_evidence_and_safety_tampering_fail_closed() -> None:
    observation = _observation()
    with pytest.raises(WindowsToastCapabilityError, match="OBSERVATION_HASH_MISMATCH"):
        probe_windows_toast_capability(replace(observation, toast_api_available=False))
    evidence = probe_windows_toast_capability(observation)
    with pytest.raises(WindowsToastCapabilityError, match="EVIDENCE_HASH_MISMATCH"):
        validate_windows_toast_capability_evidence(replace(evidence, evidence_hash="0" * 64))
    with pytest.raises(WindowsToastCapabilityError, match="EVIDENCE_SAFETY_BOUNDARY_INVALID"):
        validate_windows_toast_capability_evidence(replace(evidence, notification_sent=True))


def test_probe_has_no_toast_subprocess_control_or_mutation_surface() -> None:
    names = set(probe_windows_toast_capability.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "commit",
            "connect",
            "execute",
            "open",
            "restart",
            "shutdown",
            "show",
            "start",
            "stop",
            "systemctl",
            "toast",
            "write",
        }
    )


def _observation(
    *,
    platform="Windows",
    interactive=True,
    enabled=True,
    app_id=True,
    api=True,
    complete=True,
    duration=25,
    age=1,
    output="available",
):
    return make_windows_toast_capability_observation(
        observed_at_epoch_seconds=100,
        evidence_age_seconds=age,
        duration_milliseconds=duration,
        platform_name=platform,
        interactive_session=interactive,
        notifications_enabled=enabled,
        app_identity_registered=app_id,
        toast_api_available=api,
        complete=complete,
        probe_name="windows-toast-capability-v1",
        source_identity_hash="a" * 64,
        output=output,
    )
