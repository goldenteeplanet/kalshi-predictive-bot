from dataclasses import replace

import pytest

from kalshi_predictor.workstation.failed_post_boot_automation_disablement import (
    evaluate_failed_post_boot_automation_disablement,
)
from kalshi_predictor.workstation.operator_recovery_handoff_packet import (
    build_operator_recovery_handoff_packet,
)
from kalshi_predictor.workstation.post_boot_workstream_gate import (
    REQUIRED_PHASES,
    PostBootWorkstreamGateError,
    evaluate_post_boot_workstream_gate,
    validate_post_boot_workstream_gate_result,
)
from kalshi_predictor.workstation.recovery_outcome_notification import (
    build_recovery_outcome_notification,
)


def _chain(**overrides):
    fields = dict(
        restart_intent_hash="1" * 64,
        wsl_decision_hash="2" * 64,
        scheduler_decision_hash="3" * 64,
        database_decision_hash="4" * 64,
        invariant_decision_hash="5" * 64,
        writer_decision_hash="6" * 64,
        ui_decision_hash="7" * 64,
        created_at_epoch_seconds=100,
        wsl_verified=True,
        scheduler_verified=True,
        database_verified=True,
        invariants_verified=True,
        writer_verified=True,
        ui_verified=True,
        evidence_complete=True,
    )
    fields.update(overrides)
    notification = build_recovery_outcome_notification(**fields)
    disablement = evaluate_failed_post_boot_automation_disablement(
        notification,
        startup_task_identity_hash="a" * 64,
        supervisor_identity_hash="b" * 64,
        target_identities_verified=True,
    )
    handoff = build_operator_recovery_handoff_packet(notification, disablement)
    return notification, disablement, handoff


def test_complete_recovered_chain_passes_and_proves_success_without_authority() -> None:
    result = evaluate_post_boot_workstream_gate(*_chain(), covered_phases=REQUIRED_PHASES)
    assert (
        result.status == "PASSED"
        and result.workstream_integrity_proven
        and result.recovery_success_proven
    )
    assert not any(
        (result.recovery_authorized, result.restart_authorized, result.execution_authorized)
    )
    validate_post_boot_workstream_gate_result(result)


def test_complete_failed_chain_passes_integrity_but_not_recovery_success() -> None:
    result = evaluate_post_boot_workstream_gate(
        *_chain(database_verified=False), covered_phases=REQUIRED_PHASES
    )
    assert result.status == "PASSED" and result.workstream_integrity_proven
    assert result.outcome == "FAILED" and not result.recovery_success_proven


def test_missing_or_reordered_phase_coverage_is_incomplete() -> None:
    chain = _chain()
    assert (
        evaluate_post_boot_workstream_gate(*chain, covered_phases=REQUIRED_PHASES[:-1]).status
        == "INCOMPLETE"
    )
    assert (
        evaluate_post_boot_workstream_gate(
            *chain, covered_phases=tuple(reversed(REQUIRED_PHASES))
        ).status
        == "INCOMPLETE"
    )


def test_chain_mismatch_and_tampering_fail_closed() -> None:
    recovered = _chain()
    failed = _chain(database_verified=False)
    result = evaluate_post_boot_workstream_gate(
        recovered[0], failed[1], failed[2], covered_phases=REQUIRED_PHASES
    )
    assert result.status == "DENIED"
    with pytest.raises(PostBootWorkstreamGateError, match="INPUT_INVALID"):
        evaluate_post_boot_workstream_gate(
            recovered[0],
            replace(recovered[1], status="REQUIRED"),
            recovered[2],
            covered_phases=REQUIRED_PHASES,
        )


def test_result_tampering_and_operational_surfaces_fail_closed() -> None:
    result = evaluate_post_boot_workstream_gate(*_chain(), covered_phases=REQUIRED_PHASES)
    with pytest.raises(PostBootWorkstreamGateError, match="GATE_HASH_MISMATCH"):
        validate_post_boot_workstream_gate_result(replace(result, reasons=("FORGED",)))
    with pytest.raises(PostBootWorkstreamGateError, match="SAFETY_BOUNDARY"):
        validate_post_boot_workstream_gate_result(replace(result, restart_authorized=True))
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "schtasks",
        "systemctl",
        "restart",
        "shutdown",
        "execute",
    }
    assert forbidden.isdisjoint(evaluate_post_boot_workstream_gate.__code__.co_names)
