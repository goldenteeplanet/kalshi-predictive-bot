from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.authoritative_scheduler_health import (
    AUTHORITATIVE_UNIT,
    make_scheduler_health_observation,
    probe_authoritative_scheduler_health,
)
from kalshi_predictor.workstation.protected_invariant_recovery_gate import (
    ProtectedInvariantRecoveryGateError,
    evaluate_protected_invariant_recovery_gate,
    make_protected_invariant_observation,
    validate_protected_invariant_gate_result,
)
from kalshi_predictor.workstation.writer_exclusivity_recovery_gate import (
    evaluate_writer_exclusivity_recovery_gate,
    make_writer_inventory_observation,
)


def test_exact_invariants_and_writer_proof_pass_prerequisites_only() -> None:
    result = evaluate_protected_invariant_recovery_gate(_writer_gate(), _observation())
    validate_protected_invariant_gate_result(result)
    assert result.status == "PASSED"
    assert result.protected_invariants_proven is True
    assert result.combined_prerequisites_passed is True
    assert result.recovery_authorized is False
    assert result.service_control_authorized is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("paper_orders_count", 205),
        ("position_sizing_count", 238),
        ("advanced_risk_max_id", 240),
        ("protected_order_ticker", "OTHER"),
        ("protected_forecast_id", 523913),
        ("protected_fill_count", 2),
        ("phase3n_max_id", 232),
    ],
)
def test_each_protected_invariant_mismatch_is_denied(field: str, value: object) -> None:
    changed = replace(
        _observation(), **{field: value}, observation_hash=_rehash(field, value)
    )
    result = evaluate_protected_invariant_recovery_gate(
        _writer_gate(), changed
    )
    assert result.status == "DENIED"
    assert f"INVARIANT_MISMATCH:{field}" in result.reasons
    assert result.alert_required is True


def test_exact_freshness_boundary_passes_and_older_is_stale() -> None:
    assert evaluate_protected_invariant_recovery_gate(
        _writer_gate(age=120), _observation(age=120)
    ).status == "PASSED"
    assert evaluate_protected_invariant_recovery_gate(
        _writer_gate(), _observation(age=121)
    ).status == "STALE"


def test_incomplete_and_failed_writer_prerequisites_fail_closed() -> None:
    incomplete = evaluate_protected_invariant_recovery_gate(
        _writer_gate(), _observation(complete=False)
    )
    assert incomplete.status == "INCOMPLETE"
    denied = evaluate_protected_invariant_recovery_gate(_writer_gate(writers=0), _observation())
    assert denied.status == "DENIED"
    assert "WRITER_EXCLUSIVITY_PREREQUISITE_FAILED" in denied.reasons


def test_malformed_bound_and_tampered_inputs_fail_closed() -> None:
    with pytest.raises(ProtectedInvariantRecoveryGateError, match="OBSERVATION_FIELD_INVALID"):
        _observation(paper_orders_count=-1)
    with pytest.raises(ProtectedInvariantRecoveryGateError, match="GATE_BOUND_INVALID"):
        evaluate_protected_invariant_recovery_gate(
            _writer_gate(), _observation(), max_evidence_age_seconds=True
        )
    with pytest.raises(ProtectedInvariantRecoveryGateError, match="OBSERVATION_HASH_MISMATCH"):
        evaluate_protected_invariant_recovery_gate(
            _writer_gate(), replace(_observation(), paper_orders_count=205)
        )
    with pytest.raises(ProtectedInvariantRecoveryGateError, match="WRITER_GATE_INVALID"):
        evaluate_protected_invariant_recovery_gate(
            replace(_writer_gate(), gate_hash="0" * 64), _observation()
        )


def test_result_and_safety_tampering_fail_closed() -> None:
    result = evaluate_protected_invariant_recovery_gate(_writer_gate(), _observation())
    with pytest.raises(ProtectedInvariantRecoveryGateError, match="GATE_HASH_MISMATCH"):
        validate_protected_invariant_gate_result(replace(result, gate_hash="0" * 64))
    with pytest.raises(ProtectedInvariantRecoveryGateError, match="GATE_SAFETY_BOUNDARY_INVALID"):
        validate_protected_invariant_gate_result(replace(result, recovery_authorized=True))


def test_gate_has_no_database_service_notification_or_mutation_surface() -> None:
    names = set(evaluate_protected_invariant_recovery_gate.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "commit",
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


def _writer_gate(*, writers=1, age=1):
    scheduler = probe_authoritative_scheduler_health(
        make_scheduler_health_observation(
            observed_at_epoch_seconds=100,
            evidence_age_seconds=age,
            duration_milliseconds=25,
            unit_name=AUTHORITATIVE_UNIT,
            load_state="loaded",
            active_state="active",
            sub_state="running",
            main_pid=123,
            observed_writer_count=writers,
            complete=True,
            probe_name="systemctl-user-show-v1",
            source_identity_hash="a" * 64,
            output="healthy",
        )
    )
    identities = (AUTHORITATIVE_UNIT,) if writers == 1 else ()
    inventory = make_writer_inventory_observation(
        observed_at_epoch_seconds=100,
        evidence_age_seconds=age,
        writer_identities=identities,
        authoritative_writer_identity=AUTHORITATIVE_UNIT,
        complete=True,
        source_identity_hash="b" * 64,
    )
    return evaluate_writer_exclusivity_recovery_gate(scheduler, inventory)


def _observation(*, age=1, complete=True, **overrides):
    values = {
        "observed_at_epoch_seconds": 100,
        "evidence_age_seconds": age,
        "paper_orders_count": 204,
        "position_sizing_max_id": 239,
        "position_sizing_count": 239,
        "advanced_risk_max_id": 239,
        "advanced_risk_count": 239,
        "protected_order_id": 204,
        "protected_order_status": "filled",
        "protected_order_ticker": "KXRAINAUSM-26AUG-1",
        "protected_order_quantity": 1,
        "protected_forecast_id": 523912,
        "protected_fill_count": 1,
        "phase3m_max_id": 231,
        "phase3n_max_id": 231,
        "complete": complete,
        "source_identity_hash": "c" * 64,
    }
    values.update(overrides)
    return make_protected_invariant_observation(**values)


def _rehash(field: str, value: object) -> str:
    return _observation(**{field: value}).observation_hash
