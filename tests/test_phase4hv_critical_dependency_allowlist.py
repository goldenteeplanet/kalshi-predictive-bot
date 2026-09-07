from dataclasses import replace

import pytest
from kalshi_predictor.workstation.critical_dependency_allowlist import (
    ALLOWLISTED_DEPENDENCIES,
    CriticalDependencyAllowlistError,
    evaluate_critical_dependency,
    make_dependency_observation,
    validate_critical_dependency_decision,
)


def _observation(code="WSL_VM", **overrides):
    fields = dict(
        observation_id_hash="a" * 64,
        dependency_code=code,
        observed_at_epoch_seconds=100,
        complete=True,
    )
    fields.update(overrides)
    return make_dependency_observation(**fields)


@pytest.mark.parametrize("dependency", ALLOWLISTED_DEPENDENCIES)
def test_explicit_local_dependencies_are_allowlisted_without_authority(dependency) -> None:
    result = evaluate_critical_dependency(_observation(dependency), evaluated_at_epoch_seconds=100)
    assert result.status == "ALLOWLISTED" and result.dependency_allowlisted
    assert not any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    )
    validate_critical_dependency_decision(result)


@pytest.mark.parametrize(
    "dependency", ["NETWORK", "DNS", "EXCHANGE_API", "DATABASE", "DISK", "CLOCK", "UNKNOWN"]
)
def test_nonlocal_and_unsafe_dependencies_are_denied(dependency) -> None:
    result = evaluate_critical_dependency(_observation(dependency), evaluated_at_epoch_seconds=100)
    assert result.status == "DENIED"
    assert result.reasons == (f"DEPENDENCY_NOT_ALLOWLISTED:{dependency}",)


def test_incomplete_and_future_observations_fail_closed() -> None:
    incomplete = evaluate_critical_dependency(
        _observation(complete=False), evaluated_at_epoch_seconds=100
    )
    future = evaluate_critical_dependency(
        _observation(observed_at_epoch_seconds=101), evaluated_at_epoch_seconds=100
    )
    assert incomplete.status == "INCOMPLETE"
    assert future.status == "DENIED"


def test_malformed_and_tampered_observations_fail_closed() -> None:
    with pytest.raises(CriticalDependencyAllowlistError, match="FIELD_INVALID"):
        _observation("lower-case")
    with pytest.raises(CriticalDependencyAllowlistError, match="HASH_MISMATCH"):
        evaluate_critical_dependency(
            replace(_observation(), complete=False), evaluated_at_epoch_seconds=100
        )


def test_decision_and_safety_tampering_fail_closed() -> None:
    result = evaluate_critical_dependency(_observation(), evaluated_at_epoch_seconds=100)
    with pytest.raises(CriticalDependencyAllowlistError, match="DECISION_HASH_MISMATCH"):
        validate_critical_dependency_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(CriticalDependencyAllowlistError, match="SAFETY_BOUNDARY"):
        validate_critical_dependency_decision(replace(result, host_restart_authorized=True))


def test_evaluator_has_no_io_recovery_restart_or_execution_surface() -> None:
    forbidden = {
        "open",
        "subprocess",
        "socket",
        "restart",
        "reboot",
        "shutdown",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(evaluate_critical_dependency.__code__.co_names)
