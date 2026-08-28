from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .end_to_end_recovery_dry_run import EndToEndDryRunReport, validate_end_to_end_dry_run_report
from .mock_restart_executor import (
    MockRestartExecutionResult,
    validate_mock_restart_execution_result,
)

REHEARSAL_SCHEMA_VERSION = "phase4ko-end-to-end-mocked-restart-rehearsal-v1"
RehearsalStatus = Literal["PASSED", "FAILED", "INCOMPLETE", "TAMPERED"]


class EndToEndMockedRestartRehearsalError(ValueError):
    """Stable fail-closed mocked restart rehearsal error."""


@dataclass(frozen=True)
class MockedRestartRehearsalResult:
    status: RehearsalStatus
    reasons: tuple[str, ...]
    dry_run_report_hash: str
    mock_restart_result_hash: str
    pre_boot_identity_hash: str
    simulated_post_boot_identity_hash: str
    post_boot_workstream_gate_hash: str
    result_hash: str
    mock_only: bool = True
    boot_transition_simulated: bool = False
    post_boot_chain_verified: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    process_spawn_authorized: bool = False
    execution_authorized: bool = False


def evaluate_end_to_end_mocked_restart_rehearsal(
    dry_run_report: Any,
    mock_restart_result: Any,
    *,
    pre_boot_identity_hash: str,
    simulated_post_boot_identity_hash: str,
    post_boot_workstream_gate_hash: str,
    post_boot_workstream_passed: bool,
) -> MockedRestartRehearsalResult:
    for value in (
        pre_boot_identity_hash,
        simulated_post_boot_identity_hash,
        post_boot_workstream_gate_hash,
    ):
        _require_hash(value)
    if not isinstance(post_boot_workstream_passed, bool):
        raise EndToEndMockedRestartRehearsalError("MOCKED_REHEARSAL_FIELD_INVALID")
    if not isinstance(dry_run_report, EndToEndDryRunReport) or not isinstance(
        mock_restart_result, MockRestartExecutionResult
    ):
        raise EndToEndMockedRestartRehearsalError("MOCKED_REHEARSAL_INPUT_TYPE_INVALID")
    try:
        validate_end_to_end_dry_run_report(dry_run_report)
        validate_mock_restart_execution_result(mock_restart_result)
    except ValueError as exc:
        raise EndToEndMockedRestartRehearsalError("MOCKED_REHEARSAL_INPUT_INVALID") from exc

    transition = pre_boot_identity_hash != simulated_post_boot_identity_hash
    if not transition:
        status: RehearsalStatus = "TAMPERED"
        reasons = ["MOCKED_BOOT_IDENTITY_UNCHANGED"]
    elif dry_run_report.status != "PASSED":
        status = "INCOMPLETE"
        reasons = ["END_TO_END_DRY_RUN_PREREQUISITE_NOT_PASSED"]
    elif mock_restart_result.status == "SIMULATED_FAILURE":
        status = "FAILED"
        reasons = ["MOCK_RESTART_INJECTED_FAILURE"]
    elif mock_restart_result.status != "SIMULATED" or not mock_restart_result.mock_only:
        status = "INCOMPLETE"
        reasons = ["MOCK_RESTART_NOT_SUCCESSFULLY_SIMULATED"]
    elif not post_boot_workstream_passed:
        status = "FAILED"
        reasons = ["SIMULATED_POST_BOOT_WORKSTREAM_FAILED"]
    else:
        status = "PASSED"
        reasons = []
    passed = status == "PASSED"
    unsigned = {
        "schema_version": REHEARSAL_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "dry_run_report_hash": dry_run_report.report_hash,
        "mock_restart_result_hash": mock_restart_result.result_hash,
        "pre_boot_identity_hash": pre_boot_identity_hash,
        "simulated_post_boot_identity_hash": simulated_post_boot_identity_hash,
        "post_boot_workstream_gate_hash": post_boot_workstream_gate_hash,
        "mock_only": True,
        "boot_transition_simulated": transition,
        "post_boot_chain_verified": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "process_spawn_authorized": False,
        "execution_authorized": False,
    }
    return MockedRestartRehearsalResult(
        status=status,
        reasons=tuple(reasons),
        dry_run_report_hash=dry_run_report.report_hash,
        mock_restart_result_hash=mock_restart_result.result_hash,
        pre_boot_identity_hash=pre_boot_identity_hash,
        simulated_post_boot_identity_hash=simulated_post_boot_identity_hash,
        post_boot_workstream_gate_hash=post_boot_workstream_gate_hash,
        result_hash=_hash(unsigned),
        boot_transition_simulated=transition,
        post_boot_chain_verified=passed,
    )


def validate_mocked_restart_rehearsal_result(value: Any) -> None:
    if not isinstance(value, MockedRestartRehearsalResult):
        raise EndToEndMockedRestartRehearsalError("MOCKED_REHEARSAL_RESULT_TYPE_INVALID")
    passed = value.status == "PASSED"
    if (
        value.mock_only is not True
        or value.post_boot_chain_verified != passed
        or any(
            (
                value.recovery_authorized,
                value.restart_authorized,
                value.process_spawn_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise EndToEndMockedRestartRehearsalError("MOCKED_REHEARSAL_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = REHEARSAL_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise EndToEndMockedRestartRehearsalError("MOCKED_REHEARSAL_RESULT_HASH_MISMATCH")


def _require_hash(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise EndToEndMockedRestartRehearsalError("MOCKED_REHEARSAL_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
