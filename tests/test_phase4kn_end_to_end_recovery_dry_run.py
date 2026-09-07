from dataclasses import replace

import pytest
from kalshi_predictor.workstation.end_to_end_recovery_dry_run import (
    REQUIRED_STEPS,
    EndToEndRecoveryDryRunError,
    evaluate_end_to_end_recovery_dry_run,
    make_dry_run_step,
    validate_end_to_end_dry_run_report,
)


def _steps(*, fail_at=None, continue_after_failure=False, side_effect_at=None):
    result = []
    failed = False
    for index, name in enumerate(REQUIRED_STEPS):
        if name == fail_at:
            status = "FAIL"
            failed = True
        elif failed and not continue_after_failure:
            status = "SKIP"
        else:
            status = "PASS"
        result.append(
            make_dry_run_step(
                name=name,
                status=status,
                evidence_hash=f"{index + 1:064x}",
                side_effect_observed=name == side_effect_at,
            )
        )
    return tuple(result)


def test_complete_ordered_side_effect_free_rehearsal_passes() -> None:
    first = evaluate_end_to_end_recovery_dry_run(_steps())
    assert first == evaluate_end_to_end_recovery_dry_run(_steps())
    assert first.status == "PASSED" and first.fail_stop_proven and first.side_effect_free
    assert not any(
        (first.recovery_authorized, first.restart_authorized, first.execution_authorized)
    )
    validate_end_to_end_dry_run_report(first)


def test_failed_step_stops_all_later_steps() -> None:
    result = evaluate_end_to_end_recovery_dry_run(_steps(fail_at="CHECK_CANCELLATION"))
    assert result.status == "FAILED" and result.first_failed_step == "CHECK_CANCELLATION"
    assert result.fail_stop_proven and all(step.status == "SKIP" for step in result.steps[6:])


def test_execution_after_failure_is_rejected() -> None:
    result = evaluate_end_to_end_recovery_dry_run(
        _steps(fail_at="SEND_ALERT", continue_after_failure=True)
    )
    assert result.status == "FAILED" and not result.fail_stop_proven
    assert "DRY_RUN_FAIL_STOP_VIOLATED" in result.reasons


def test_missing_reordered_or_side_effecting_steps_fail_closed() -> None:
    assert evaluate_end_to_end_recovery_dry_run(_steps()[:-1]).status == "INCOMPLETE"
    assert evaluate_end_to_end_recovery_dry_run(tuple(reversed(_steps()))).status == "INCOMPLETE"
    result = evaluate_end_to_end_recovery_dry_run(
        _steps(side_effect_at="SIMULATE_NON_FORCED_RESTART")
    )
    assert result.status == "FAILED" and not result.side_effect_free


def test_step_report_tampering_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(EndToEndRecoveryDryRunError, match="STEP_FIELD_INVALID"):
        make_dry_run_step(name="UNKNOWN", status="PASS", evidence_hash="1" * 64)
    report = evaluate_end_to_end_recovery_dry_run(_steps())
    with pytest.raises(EndToEndRecoveryDryRunError, match="REPORT_HASH_MISMATCH"):
        validate_end_to_end_dry_run_report(replace(report, first_failed_step="FORGED"))
    with pytest.raises(EndToEndRecoveryDryRunError, match="SAFETY_BOUNDARY"):
        validate_end_to_end_dry_run_report(replace(report, restart_authorized=True))
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "systemctl",
        "restart",
        "shutdown",
        "execute",
        "order",
    }
    assert forbidden.isdisjoint(evaluate_end_to_end_recovery_dry_run.__code__.co_names)
