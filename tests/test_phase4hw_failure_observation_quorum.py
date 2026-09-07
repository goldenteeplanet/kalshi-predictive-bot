from dataclasses import replace

import pytest
from kalshi_predictor.workstation.critical_dependency_allowlist import (
    evaluate_critical_dependency,
    make_dependency_observation,
)
from kalshi_predictor.workstation.failure_observation_quorum import (
    FailureObservationQuorumError,
    evaluate_failure_observation_quorum,
    make_failure_observation,
    validate_failure_quorum_decision,
)


def _allowlisted(code="WSL_VM"):
    item = make_dependency_observation(
        observation_id_hash="a" * 64,
        dependency_code=code,
        observed_at_epoch_seconds=100,
        complete=True,
    )
    return evaluate_critical_dependency(item, evaluated_at_epoch_seconds=100)


def _obs(n, source, **overrides):
    fields = dict(
        observation_id_hash=f"{n:x}" * 64,
        source_code=source,
        dependency_code="WSL_VM",
        observed_at_epoch_seconds=100,
        failure_observed=True,
        complete=True,
    )
    fields.update(overrides)
    return make_failure_observation(**fields)


def test_two_distinct_fresh_sources_form_deterministic_non_authorizing_quorum() -> None:
    records = [_obs(1, "WSL_PROBE"), _obs(2, "SCHEDULER_PROBE")]
    first = evaluate_failure_observation_quorum(
        _allowlisted(), records, evaluated_at_epoch_seconds=100
    )
    second = evaluate_failure_observation_quorum(
        _allowlisted(), list(reversed(records)), evaluated_at_epoch_seconds=100
    )
    assert first == second and first.status == "QUORUM" and first.failure_quorum_proven
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_failure_quorum_decision(first)


def test_same_source_repetition_and_healthy_observation_do_not_form_quorum() -> None:
    same = [_obs(1, "WSL_PROBE"), _obs(2, "WSL_PROBE")]
    mixed = [_obs(1, "WSL_PROBE"), _obs(2, "SCHEDULER_PROBE", failure_observed=False)]
    assert (
        evaluate_failure_observation_quorum(
            _allowlisted(), same, evaluated_at_epoch_seconds=100
        ).status
        == "NO_QUORUM"
    )
    assert (
        evaluate_failure_observation_quorum(
            _allowlisted(), mixed, evaluated_at_epoch_seconds=100
        ).status
        == "NO_QUORUM"
    )


def test_exact_freshness_boundary_passes_then_becomes_stale() -> None:
    records = [
        _obs(1, "A", observed_at_epoch_seconds=100),
        _obs(2, "B", observed_at_epoch_seconds=100),
    ]
    assert (
        evaluate_failure_observation_quorum(
            _allowlisted(), records, evaluated_at_epoch_seconds=220
        ).status
        == "QUORUM"
    )
    assert (
        evaluate_failure_observation_quorum(
            _allowlisted(), records, evaluated_at_epoch_seconds=221
        ).status
        == "NO_QUORUM"
    )


def test_incomplete_future_mixed_dependency_and_duplicate_ids_fail_closed() -> None:
    assert (
        evaluate_failure_observation_quorum(
            _allowlisted(), [_obs(1, "A", complete=False)], evaluated_at_epoch_seconds=100
        ).status
        == "INCOMPLETE"
    )
    assert (
        evaluate_failure_observation_quorum(
            _allowlisted(),
            [_obs(1, "A", observed_at_epoch_seconds=101)],
            evaluated_at_epoch_seconds=100,
        ).status
        == "DENIED"
    )
    assert (
        evaluate_failure_observation_quorum(
            _allowlisted(),
            [_obs(1, "A", dependency_code="DATABASE")],
            evaluated_at_epoch_seconds=100,
        ).status
        == "TAMPERED"
    )
    duplicate = [_obs(1, "A"), _obs(1, "B")]
    assert (
        evaluate_failure_observation_quorum(
            _allowlisted(), duplicate, evaluated_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )


def test_denied_allowlist_and_tampering_fail_closed() -> None:
    denied = _allowlisted("NETWORK")
    assert (
        evaluate_failure_observation_quorum(denied, [], evaluated_at_epoch_seconds=100).status
        == "DENIED"
    )
    with pytest.raises(FailureObservationQuorumError, match="HASH_MISMATCH"):
        evaluate_failure_observation_quorum(
            _allowlisted(), [replace(_obs(1, "A"), complete=False)], evaluated_at_epoch_seconds=100
        )


def test_decision_safety_tampering_and_operational_surfaces_are_rejected() -> None:
    result = evaluate_failure_observation_quorum(
        _allowlisted(), [_obs(1, "A"), _obs(2, "B")], evaluated_at_epoch_seconds=100
    )
    with pytest.raises(FailureObservationQuorumError, match="DECISION_HASH_MISMATCH"):
        validate_failure_quorum_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(FailureObservationQuorumError, match="SAFETY_BOUNDARY"):
        validate_failure_quorum_decision(replace(result, host_restart_authorized=True))
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
    assert forbidden.isdisjoint(evaluate_failure_observation_quorum.__code__.co_names)
