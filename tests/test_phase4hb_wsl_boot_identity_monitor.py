from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.wsl_boot_identity_monitor import (
    WslBootIdentityMonitorError,
    make_wsl_boot_identity_observation,
    monitor_wsl_boot_identity,
    validate_wsl_boot_identity_monitor_result,
)


def test_stable_history_is_deterministic_read_only_and_redacted() -> None:
    first = monitor_wsl_boot_identity(list(reversed(_observations())))
    second = monitor_wsl_boot_identity(_observations())
    validate_wsl_boot_identity_monitor_result(first)
    assert first.status == "STABLE"
    assert first.result_hash == second.result_hash
    assert first.current_boot_identity_hash != BOOT_A
    assert first.host_restart_authorized is False


def test_boot_change_requires_alert_but_never_authorizes_recovery() -> None:
    result = monitor_wsl_boot_identity([_observation(1, 100), _observation(2, 130, boot=BOOT_B)])
    assert result.status == "CHANGED"
    assert result.transition_count == 1
    assert result.reasons == ("WSL_BOOT_IDENTITY_CHANGED",)
    assert result.alert_required is True
    assert result.recovery_authorized is False


def test_empty_and_resource_bounds_fail_closed() -> None:
    with pytest.raises(WslBootIdentityMonitorError, match="OBSERVATIONS_EMPTY"):
        monitor_wsl_boot_identity([])
    with pytest.raises(WslBootIdentityMonitorError, match="OBSERVATION_BOUND_EXCEEDED"):
        monitor_wsl_boot_identity(_observations(), max_observations=1)
    with pytest.raises(WslBootIdentityMonitorError, match="MONITOR_BOUND_INVALID"):
        monitor_wsl_boot_identity(_observations(), max_observations=True)


def test_exact_freshness_boundary_and_staleness() -> None:
    assert monitor_wsl_boot_identity([_observation(1, 100, age=120)]).status == "STABLE"
    stale = monitor_wsl_boot_identity([_observation(1, 100, age=121)])
    assert stale.status == "STALE"
    assert stale.alert_required is True


def test_partial_and_malformed_evidence_fail_closed() -> None:
    degraded = monitor_wsl_boot_identity([_observation(1, 100, complete=False)])
    assert degraded.status == "DEGRADED"
    with pytest.raises(WslBootIdentityMonitorError, match="OBSERVATION_FIELD_INVALID"):
        _observation(1, 100, boot="")
    with pytest.raises(WslBootIdentityMonitorError, match="OBSERVATION_SEQUENCE_GAP"):
        monitor_wsl_boot_identity([_observation(1, 100), _observation(3, 130)])
    with pytest.raises(WslBootIdentityMonitorError, match="OBSERVATION_TIME_NOT_MONOTONIC"):
        monitor_wsl_boot_identity([_observation(1, 100), _observation(2, 100)])


def test_lineage_observation_and_result_tampering_fail_closed() -> None:
    with pytest.raises(WslBootIdentityMonitorError, match="OBSERVATION_LINEAGE_MIXED"):
        monitor_wsl_boot_identity([_observation(1, 100), _observation(2, 130, source="b" * 64)])
    item = _observation(1, 100)
    with pytest.raises(WslBootIdentityMonitorError, match="OBSERVATION_HASH_MISMATCH"):
        monitor_wsl_boot_identity([replace(item, boot_identity=BOOT_B)])
    result = monitor_wsl_boot_identity(_observations())
    with pytest.raises(WslBootIdentityMonitorError, match="MONITOR_RESULT_HASH_MISMATCH"):
        validate_wsl_boot_identity_monitor_result(replace(result, result_hash="0" * 64))
    with pytest.raises(WslBootIdentityMonitorError, match="MONITOR_SAFETY_BOUNDARY_INVALID"):
        validate_wsl_boot_identity_monitor_result(replace(result, host_restart_authorized=True))


def test_monitor_has_no_query_control_notification_or_mutation_surface() -> None:
    names = set(monitor_wsl_boot_identity.__code__.co_names)
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


BOOT_A = "11111111-1111-4111-8111-111111111111"
BOOT_B = "22222222-2222-4222-8222-222222222222"


def _observation(
    sequence: int,
    observed_at: int,
    *,
    boot: str = BOOT_A,
    complete: bool = True,
    source: str = "a" * 64,
    age: int = 1,
):
    return make_wsl_boot_identity_observation(
        sequence=sequence,
        observed_at_epoch_seconds=observed_at,
        boot_identity=boot,
        complete=complete,
        source_identity_hash=source,
        evidence_age_seconds=age,
    )


def _observations():
    return [_observation(1, 100), _observation(2, 130)]
