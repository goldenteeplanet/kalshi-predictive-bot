from dataclasses import replace

import pytest
from kalshi_predictor.workstation.critical_dependency_allowlist import (
    evaluate_critical_dependency,
    make_dependency_observation,
)
from kalshi_predictor.workstation.failure_observation_quorum import (
    evaluate_failure_observation_quorum,
    make_failure_observation,
)
from kalshi_predictor.workstation.failure_persistence_window import (
    FailurePersistenceWindowError,
    evaluate_failure_persistence_window,
    validate_failure_persistence_decision,
)


def _quorum(at, suffix, code="WSL_VM"):
    dep = make_dependency_observation(
        observation_id_hash="a" * 64,
        dependency_code=code,
        observed_at_epoch_seconds=1,
        complete=True,
    )
    allowed = evaluate_critical_dependency(dep, evaluated_at_epoch_seconds=1)
    records = [
        make_failure_observation(
            observation_id_hash=str(suffix) * 64,
            source_code="A",
            dependency_code=code,
            observed_at_epoch_seconds=at,
            failure_observed=True,
            complete=True,
        ),
        make_failure_observation(
            observation_id_hash=str(suffix + 1) * 64,
            source_code="B",
            dependency_code=code,
            observed_at_epoch_seconds=at,
            failure_observed=True,
            complete=True,
        ),
    ]
    return evaluate_failure_observation_quorum(allowed, records, evaluated_at_epoch_seconds=at)


def test_two_quorums_at_exact_span_prove_persistence_without_authority() -> None:
    records = [_quorum(100, 1), _quorum(160, 3)]
    result = evaluate_failure_persistence_window(
        records, dependency_code="WSL_VM", evaluated_at_epoch_seconds=160
    )
    assert result.status == "PERSISTENT" and result.observed_span_seconds == 60
    assert not any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    )
    validate_failure_persistence_decision(result)


def test_order_is_deterministic_and_single_or_short_history_is_transient() -> None:
    records = [_quorum(100, 1), _quorum(159, 3)]
    assert (
        evaluate_failure_persistence_window(
            records, dependency_code="WSL_VM", evaluated_at_epoch_seconds=159
        ).status
        == "TRANSIENT"
    )
    assert (
        evaluate_failure_persistence_window(
            [records[0]], dependency_code="WSL_VM", evaluated_at_epoch_seconds=100
        ).status
        == "TRANSIENT"
    )
    a = evaluate_failure_persistence_window(
        [_quorum(100, 1), _quorum(160, 3)], dependency_code="WSL_VM", evaluated_at_epoch_seconds=160
    )
    b = evaluate_failure_persistence_window(
        [_quorum(160, 3), _quorum(100, 1)], dependency_code="WSL_VM", evaluated_at_epoch_seconds=160
    )
    assert a == b


def test_latest_age_exact_boundary_passes_then_stales() -> None:
    records = [_quorum(100, 1), _quorum(160, 3)]
    assert (
        evaluate_failure_persistence_window(
            records, dependency_code="WSL_VM", evaluated_at_epoch_seconds=280
        ).status
        == "PERSISTENT"
    )
    assert (
        evaluate_failure_persistence_window(
            records, dependency_code="WSL_VM", evaluated_at_epoch_seconds=281
        ).status
        == "TRANSIENT"
    )


def test_duplicate_mixed_future_and_invalid_history_fail_closed() -> None:
    item = _quorum(100, 1)
    assert (
        evaluate_failure_persistence_window(
            [item, item], dependency_code="WSL_VM", evaluated_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )
    mixed = [_quorum(100, 1), _quorum(160, 3, "KALSHI_SCHEDULER")]
    assert (
        evaluate_failure_persistence_window(
            mixed, dependency_code="WSL_VM", evaluated_at_epoch_seconds=160
        ).status
        == "TAMPERED"
    )
    assert (
        evaluate_failure_persistence_window(
            [_quorum(101, 1)], dependency_code="WSL_VM", evaluated_at_epoch_seconds=100
        ).status
        == "DENIED"
    )


def test_tampering_bounds_safety_and_operational_surfaces_fail_closed() -> None:
    item = _quorum(100, 1)
    with pytest.raises(FailurePersistenceWindowError, match="DECISION_INVALID"):
        evaluate_failure_persistence_window(
            [replace(item, reasons=("FORGED",))],
            dependency_code="WSL_VM",
            evaluated_at_epoch_seconds=100,
        )
    result = evaluate_failure_persistence_window(
        [item], dependency_code="WSL_VM", evaluated_at_epoch_seconds=100
    )
    with pytest.raises(FailurePersistenceWindowError, match="RESULT_HASH_MISMATCH"):
        validate_failure_persistence_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(FailurePersistenceWindowError, match="SAFETY_BOUNDARY"):
        validate_failure_persistence_decision(replace(result, host_restart_authorized=True))
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
    assert forbidden.isdisjoint(evaluate_failure_persistence_window.__code__.co_names)
