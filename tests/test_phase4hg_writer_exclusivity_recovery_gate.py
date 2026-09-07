from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.workstation.authoritative_scheduler_health import (
    AUTHORITATIVE_UNIT,
    make_scheduler_health_observation,
    probe_authoritative_scheduler_health,
)
from kalshi_predictor.workstation.writer_exclusivity_recovery_gate import (
    WriterExclusivityRecoveryGateError,
    evaluate_writer_exclusivity_recovery_gate,
    make_writer_inventory_observation,
    validate_writer_exclusivity_gate_result,
)


def test_exclusive_authoritative_writer_passes_prerequisite_only() -> None:
    result = evaluate_writer_exclusivity_recovery_gate(_scheduler(), _inventory())
    validate_writer_exclusivity_gate_result(result)
    assert result.status == "PASSED"
    assert result.writer_exclusivity_proven is True
    assert result.prerequisite_gate_passed is True
    assert result.recovery_authorized is False
    assert result.service_control_authorized is False


def test_zero_multiple_and_unexpected_writers_are_denied() -> None:
    missing = evaluate_writer_exclusivity_recovery_gate(_scheduler(), _inventory(writers=()))
    assert missing.status == "DENIED"
    assert "WRITER_MISSING" in missing.reasons
    multiple = evaluate_writer_exclusivity_recovery_gate(
        _scheduler(writers=2), _inventory(writers=(AUTHORITATIVE_UNIT, "rogue.service"))
    )
    assert "MULTIPLE_WRITERS_OBSERVED" in multiple.reasons
    unexpected = evaluate_writer_exclusivity_recovery_gate(
        _scheduler(), _inventory(writers=("rogue.service",))
    )
    assert "UNEXPECTED_WRITER_OBSERVED" in unexpected.reasons


def test_exact_freshness_boundary_passes_and_older_is_stale() -> None:
    exact = evaluate_writer_exclusivity_recovery_gate(_scheduler(age=120), _inventory(age=120))
    assert exact.status == "PASSED"
    stale = evaluate_writer_exclusivity_recovery_gate(_scheduler(), _inventory(age=121))
    assert stale.status == "STALE"
    assert stale.alert_required is True


def test_incomplete_and_identity_mismatch_fail_closed() -> None:
    incomplete = evaluate_writer_exclusivity_recovery_gate(_scheduler(), _inventory(complete=False))
    assert incomplete.status == "INCOMPLETE"
    mismatch = evaluate_writer_exclusivity_recovery_gate(
        _scheduler(), _inventory(authoritative="other.service")
    )
    assert mismatch.status == "DENIED"
    assert "AUTHORITATIVE_WRITER_IDENTITY_MISMATCH" in mismatch.reasons


def test_malformed_duplicate_and_bound_inputs_fail_closed() -> None:
    with pytest.raises(WriterExclusivityRecoveryGateError, match="WRITER_IDENTITY_DUPLICATE"):
        _inventory(writers=(AUTHORITATIVE_UNIT, AUTHORITATIVE_UNIT))
    with pytest.raises(WriterExclusivityRecoveryGateError, match="GATE_BOUND_INVALID"):
        evaluate_writer_exclusivity_recovery_gate(
            _scheduler(), _inventory(), max_evidence_age_seconds=True
        )


def test_inventory_scheduler_result_and_safety_tampering_fail_closed() -> None:
    inventory = _inventory()
    with pytest.raises(WriterExclusivityRecoveryGateError, match="INVENTORY_HASH_MISMATCH"):
        evaluate_writer_exclusivity_recovery_gate(
            _scheduler(), replace(inventory, writer_identities=("rogue.service",))
        )
    scheduler = _scheduler()
    with pytest.raises(WriterExclusivityRecoveryGateError, match="SCHEDULER_EVIDENCE_INVALID"):
        evaluate_writer_exclusivity_recovery_gate(
            replace(scheduler, evidence_hash="0" * 64), inventory
        )
    result = evaluate_writer_exclusivity_recovery_gate(scheduler, inventory)
    with pytest.raises(WriterExclusivityRecoveryGateError, match="GATE_HASH_MISMATCH"):
        validate_writer_exclusivity_gate_result(replace(result, gate_hash="0" * 64))
    with pytest.raises(WriterExclusivityRecoveryGateError, match="GATE_SAFETY_BOUNDARY_INVALID"):
        validate_writer_exclusivity_gate_result(replace(result, recovery_authorized=True))


def test_gate_has_no_query_control_notification_or_mutation_surface() -> None:
    names = set(evaluate_writer_exclusivity_recovery_gate.__code__.co_names)
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


def _scheduler(*, writers=1, age=1):
    observation = make_scheduler_health_observation(
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
    return probe_authoritative_scheduler_health(observation)


def _inventory(
    *,
    writers=(AUTHORITATIVE_UNIT,),
    authoritative=AUTHORITATIVE_UNIT,
    complete=True,
    age=1,
):
    return make_writer_inventory_observation(
        observed_at_epoch_seconds=100,
        evidence_age_seconds=age,
        writer_identities=writers,
        authoritative_writer_identity=authoritative,
        complete=complete,
        source_identity_hash="b" * 64,
    )
