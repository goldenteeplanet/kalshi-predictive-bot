from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

EXECUTOR_SCHEMA_VERSION = "phase4jk-mock-restart-executor-v1"
ALLOWLISTED_EXECUTABLE = "shutdown.exe"
ALLOWLISTED_ARGUMENTS = ("/r", "/t", "0")
SimulationStatus = Literal["SIMULATED", "SIMULATED_FAILURE", "DENIED", "INCOMPLETE", "TAMPERED"]


class MockRestartExecutorError(ValueError):
    """Stable fail-closed mock restart executor error."""


@dataclass(frozen=True)
class MockRestartExecutionRequest:
    simulation_id_hash: str
    incident_id_hash: str
    command_preview_hash: str
    executable: str
    arguments: tuple[str, ...]
    injected_exit_code: int
    simulation_mode: bool
    dry_run: bool
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class MockRestartExecutionResult:
    status: SimulationStatus
    reasons: tuple[str, ...]
    simulation_id_hash: str
    incident_id_hash: str
    request_hash: str
    invocation_hash: str
    simulated_exit_code: int
    result_hash: str
    read_only: bool = True
    mock_only: bool = True
    invocation_recorded: bool = False
    restart_effect_simulated: bool = False
    restart_authorized: bool = False
    process_spawn_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_mock_restart_execution_request(**fields: Any) -> MockRestartExecutionRequest:
    normalized = dict(fields)
    if isinstance(normalized.get("arguments"), list):
        normalized["arguments"] = tuple(normalized["arguments"])
    _validate_fields(normalized)
    unsigned = {**normalized, "arguments": list(normalized["arguments"])}
    return MockRestartExecutionRequest(**normalized, request_hash=_hash(unsigned))


def execute_mock_restart(request: Any) -> MockRestartExecutionResult:
    item = _validated_request(request)
    exact = (
        item.executable.casefold() == ALLOWLISTED_EXECUTABLE
        and item.arguments == ALLOWLISTED_ARGUMENTS
    )
    forced = any(value.casefold() in {"/f", "-force", "/force"} for value in item.arguments)
    if not item.complete:
        status: SimulationStatus = "INCOMPLETE"
        reasons = ["MOCK_RESTART_REQUEST_INCOMPLETE"]
    elif forced:
        status = "TAMPERED"
        reasons = ["MOCK_RESTART_FORCE_FLAG_REFUSED"]
    elif not item.simulation_mode or not item.dry_run:
        status = "DENIED"
        reasons = ["MOCK_RESTART_SIMULATION_AND_DRY_RUN_REQUIRED"]
    elif not exact:
        status = "DENIED"
        reasons = ["MOCK_RESTART_COMMAND_NOT_ALLOWLISTED"]
    elif item.injected_exit_code == 0:
        status = "SIMULATED"
        reasons = []
    else:
        status = "SIMULATED_FAILURE"
        reasons = [f"MOCK_RESTART_EXIT_CODE:{item.injected_exit_code}"]
    recorded = status in {"SIMULATED", "SIMULATED_FAILURE"}
    invocation_hash = _hash(
        {
            "executable": ALLOWLISTED_EXECUTABLE,
            "arguments": list(ALLOWLISTED_ARGUMENTS),
            "injected_exit_code": item.injected_exit_code,
            "mock_only": True,
        }
    )
    unsigned = {
        "schema_version": EXECUTOR_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "simulation_id_hash": item.simulation_id_hash,
        "incident_id_hash": item.incident_id_hash,
        "request_hash": item.request_hash,
        "invocation_hash": invocation_hash,
        "simulated_exit_code": item.injected_exit_code,
        "read_only": True,
        "mock_only": True,
        "invocation_recorded": recorded,
        "restart_effect_simulated": status == "SIMULATED",
        "restart_authorized": False,
        "process_spawn_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return MockRestartExecutionResult(
        status=status,
        reasons=tuple(reasons),
        simulation_id_hash=item.simulation_id_hash,
        incident_id_hash=item.incident_id_hash,
        request_hash=item.request_hash,
        invocation_hash=invocation_hash,
        simulated_exit_code=item.injected_exit_code,
        result_hash=_hash(unsigned),
        invocation_recorded=recorded,
        restart_effect_simulated=status == "SIMULATED",
    )


def validate_mock_restart_execution_result(value: Any) -> None:
    if not isinstance(value, MockRestartExecutionResult):
        raise MockRestartExecutorError("MOCK_RESTART_RESULT_TYPE_INVALID")
    recorded = value.status in {"SIMULATED", "SIMULATED_FAILURE"}
    if (
        value.read_only is not True
        or value.mock_only is not True
        or value.invocation_recorded != recorded
        or value.restart_effect_simulated != (value.status == "SIMULATED")
        or any(
            (
                value.restart_authorized,
                value.process_spawn_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise MockRestartExecutorError("MOCK_RESTART_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = EXECUTOR_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise MockRestartExecutorError("MOCK_RESTART_RESULT_HASH_MISMATCH")


def _validated_request(value: Any) -> MockRestartExecutionRequest:
    if not isinstance(value, MockRestartExecutionRequest):
        raise MockRestartExecutorError("MOCK_RESTART_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    unsigned["arguments"] = tuple(unsigned["arguments"])
    _validate_fields(unsigned)
    unsigned["arguments"] = list(unsigned["arguments"])
    if supplied != _hash(unsigned):
        raise MockRestartExecutorError("MOCK_RESTART_REQUEST_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "simulation_id_hash",
        "incident_id_hash",
        "command_preview_hash",
        "executable",
        "arguments",
        "injected_exit_code",
        "simulation_mode",
        "dry_run",
        "complete",
    }
    if set(fields) != required:
        raise MockRestartExecutorError("MOCK_RESTART_FIELD_INVALID")
    for key in ("simulation_id_hash", "incident_id_hash", "command_preview_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise MockRestartExecutorError("MOCK_RESTART_FIELD_INVALID")
    if (
        not isinstance(fields["executable"], str)
        or len(fields["executable"]) > 64
        or not isinstance(fields["arguments"], tuple)
        or len(fields["arguments"]) > 8
        or any(not isinstance(item, str) or len(item) > 64 for item in fields["arguments"])
        or isinstance(fields["injected_exit_code"], bool)
        or not isinstance(fields["injected_exit_code"], int)
        or not 0 <= fields["injected_exit_code"] <= 255
    ):
        raise MockRestartExecutorError("MOCK_RESTART_FIELD_INVALID")
    for key in ("simulation_mode", "dry_run", "complete"):
        if not isinstance(fields[key], bool):
            raise MockRestartExecutorError("MOCK_RESTART_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
