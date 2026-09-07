from dataclasses import replace

import pytest

from kalshi_predictor.workstation.network_failure_non_restart_rule import (
    NETWORK_FAILURE_CODES,
    NetworkFailureNonRestartRuleError,
    evaluate_network_failure_non_restart_rule,
    make_network_failure_evidence,
    validate_network_failure_decision,
)


def _evidence(**overrides):
    fields = dict(
        probe_id_hash="a" * 64,
        observed_at_epoch_seconds=100,
        endpoint_id_hash="b" * 64,
        failure_code="NONE",
        connectivity_succeeded=True,
        complete=True,
    )
    fields.update(overrides)
    return make_network_failure_evidence(**fields)


def test_coherent_success_is_healthy_deterministic_and_non_authorizing() -> None:
    first = evaluate_network_failure_non_restart_rule(_evidence(), evaluated_at_epoch_seconds=100)
    second = evaluate_network_failure_non_restart_rule(_evidence(), evaluated_at_epoch_seconds=100)
    assert first == second and first.status == "HEALTHY" and first.connectivity_proven
    assert not any(
        (
            first.restart_eligible,
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_network_failure_decision(first)


@pytest.mark.parametrize("failure_code", sorted(NETWORK_FAILURE_CODES))
def test_every_network_failure_class_is_retryable_but_never_restartable(failure_code) -> None:
    result = evaluate_network_failure_non_restart_rule(
        _evidence(connectivity_succeeded=False, failure_code=failure_code),
        evaluated_at_epoch_seconds=100,
    )
    assert result.status == "NETWORK_FAILURE" and result.retry_policy_required
    assert result.restart_eligible is False and result.host_restart_authorized is False


def test_contradictory_unknown_incomplete_future_and_stale_evidence_fail_closed() -> None:
    contradictory = evaluate_network_failure_non_restart_rule(
        _evidence(failure_code="DNS_FAILURE"), evaluated_at_epoch_seconds=100
    )
    unknown = evaluate_network_failure_non_restart_rule(
        _evidence(connectivity_succeeded=False, failure_code="MYSTERY"),
        evaluated_at_epoch_seconds=100,
    )
    incomplete = evaluate_network_failure_non_restart_rule(
        _evidence(complete=False), evaluated_at_epoch_seconds=100
    )
    future = evaluate_network_failure_non_restart_rule(
        _evidence(observed_at_epoch_seconds=101), evaluated_at_epoch_seconds=100
    )
    stale = evaluate_network_failure_non_restart_rule(_evidence(), evaluated_at_epoch_seconds=221)
    assert contradictory.status == unknown.status == stale.status == "UNKNOWN"
    assert incomplete.status == "INCOMPLETE" and future.status == "TAMPERED"


def test_exact_freshness_boundary_passes_then_stales() -> None:
    assert (
        evaluate_network_failure_non_restart_rule(
            _evidence(), evaluated_at_epoch_seconds=220
        ).status
        == "HEALTHY"
    )
    assert (
        evaluate_network_failure_non_restart_rule(
            _evidence(), evaluated_at_epoch_seconds=221
        ).status
        == "UNKNOWN"
    )


def test_tampering_safety_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(NetworkFailureNonRestartRuleError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_network_failure_non_restart_rule(
            replace(_evidence(), connectivity_succeeded=False), evaluated_at_epoch_seconds=100
        )
    result = evaluate_network_failure_non_restart_rule(_evidence(), evaluated_at_epoch_seconds=100)
    with pytest.raises(NetworkFailureNonRestartRuleError, match="DECISION_HASH_MISMATCH"):
        validate_network_failure_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(NetworkFailureNonRestartRuleError, match="SAFETY_BOUNDARY"):
        validate_network_failure_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "connect",
        "send",
        "requests",
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
    assert forbidden.isdisjoint(evaluate_network_failure_non_restart_rule.__code__.co_names)
