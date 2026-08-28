from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

ADAPTER_SCHEMA_VERSION = "phase4jh-non-forced-restart-command-adapter-v1"
EXECUTABLE = "shutdown.exe"
COMMAND_ARGUMENTS = ("/r", "/t", "0")
AdapterStatus = Literal["PREVIEW_READY", "DENIED", "INCOMPLETE", "TAMPERED"]


class NonForcedRestartCommandAdapterError(ValueError):
    """Stable fail-closed non-forced restart command adapter error."""


@dataclass(frozen=True)
class RestartCommandRequest:
    incident_id_hash: str
    restart_intent_hash: str
    warning_decision_hash: str
    cooldown_decision_hash: str
    budget_decision_hash: str
    loop_breaker_decision_hash: str
    executable: str
    arguments: tuple[str, ...]
    dry_run: bool
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class RestartCommandPreview:
    status: AdapterStatus
    reasons: tuple[str, ...]
    incident_id_hash: str
    request_hash: str
    executable: str
    arguments: tuple[str, ...]
    command_hash: str
    preview_hash: str
    read_only: bool = True
    dry_run: bool = True
    non_forced_proven: bool = False
    command_preview_ready: bool = False
    restart_authorized: bool = False
    process_spawn_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_command_request(**fields: Any) -> RestartCommandRequest:
    normalized = dict(fields)
    if isinstance(normalized.get("arguments"), list):
        normalized["arguments"] = tuple(normalized["arguments"])
    _validate_fields(normalized)
    unsigned = {**normalized, "arguments": list(normalized["arguments"])}
    return RestartCommandRequest(**normalized, request_hash=_hash(unsigned))


def build_non_forced_restart_command_preview(request: Any) -> RestartCommandPreview:
    item = _validated_request(request)
    forbidden = any(
        argument.casefold() in {"/f", "-force", "/force"} for argument in item.arguments
    )
    exact = item.executable.casefold() == EXECUTABLE and item.arguments == COMMAND_ARGUMENTS
    if not item.complete:
        status: AdapterStatus = "INCOMPLETE"
        reasons = ["RESTART_COMMAND_REQUEST_INCOMPLETE"]
    elif forbidden:
        status = "TAMPERED"
        reasons = ["RESTART_COMMAND_FORCE_FLAG_REFUSED"]
    elif not item.dry_run:
        status = "DENIED"
        reasons = ["RESTART_COMMAND_DRY_RUN_REQUIRED"]
    elif not exact:
        status = "DENIED"
        reasons = ["RESTART_COMMAND_NOT_ALLOWLISTED"]
    else:
        status = "PREVIEW_READY"
        reasons = []
    ready = status == "PREVIEW_READY"
    command_hash = _hash({"executable": EXECUTABLE, "arguments": list(COMMAND_ARGUMENTS)})
    unsigned = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "incident_id_hash": item.incident_id_hash,
        "request_hash": item.request_hash,
        "executable": EXECUTABLE,
        "arguments": list(COMMAND_ARGUMENTS),
        "command_hash": command_hash,
        "read_only": True,
        "dry_run": True,
        "non_forced_proven": ready,
        "command_preview_ready": ready,
        "restart_authorized": False,
        "process_spawn_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartCommandPreview(
        status=status,
        reasons=tuple(reasons),
        incident_id_hash=item.incident_id_hash,
        request_hash=item.request_hash,
        executable=EXECUTABLE,
        arguments=COMMAND_ARGUMENTS,
        command_hash=command_hash,
        preview_hash=_hash(unsigned),
        non_forced_proven=ready,
        command_preview_ready=ready,
    )


def validate_restart_command_preview(value: Any) -> None:
    if not isinstance(value, RestartCommandPreview):
        raise NonForcedRestartCommandAdapterError("RESTART_COMMAND_PREVIEW_TYPE_INVALID")
    ready = value.status == "PREVIEW_READY"
    if (
        value.read_only is not True
        or value.dry_run is not True
        or value.executable != EXECUTABLE
        or value.arguments != COMMAND_ARGUMENTS
        or value.non_forced_proven != ready
        or value.command_preview_ready != ready
        or any(
            (
                value.restart_authorized,
                value.process_spawn_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise NonForcedRestartCommandAdapterError("RESTART_COMMAND_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("preview_hash")
    unsigned["schema_version"] = ADAPTER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["arguments"] = list(unsigned["arguments"])
    if value.preview_hash != _hash(unsigned):
        raise NonForcedRestartCommandAdapterError("RESTART_COMMAND_PREVIEW_HASH_MISMATCH")


def _validated_request(value: Any) -> RestartCommandRequest:
    if not isinstance(value, RestartCommandRequest):
        raise NonForcedRestartCommandAdapterError("RESTART_COMMAND_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    unsigned["arguments"] = tuple(unsigned["arguments"])
    _validate_fields(unsigned)
    unsigned["arguments"] = list(unsigned["arguments"])
    if supplied != _hash(unsigned):
        raise NonForcedRestartCommandAdapterError("RESTART_COMMAND_REQUEST_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "incident_id_hash",
        "restart_intent_hash",
        "warning_decision_hash",
        "cooldown_decision_hash",
        "budget_decision_hash",
        "loop_breaker_decision_hash",
        "executable",
        "arguments",
        "dry_run",
        "complete",
    }
    if set(fields) != required:
        raise NonForcedRestartCommandAdapterError("RESTART_COMMAND_FIELD_INVALID")
    for key in (
        "incident_id_hash",
        "restart_intent_hash",
        "warning_decision_hash",
        "cooldown_decision_hash",
        "budget_decision_hash",
        "loop_breaker_decision_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise NonForcedRestartCommandAdapterError("RESTART_COMMAND_FIELD_INVALID")
    if (
        not isinstance(fields["executable"], str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", fields["executable"]) is None
        or not isinstance(fields["arguments"], tuple)
        or len(fields["arguments"]) > 8
        or any(not isinstance(item, str) or len(item) > 64 for item in fields["arguments"])
    ):
        raise NonForcedRestartCommandAdapterError("RESTART_COMMAND_FIELD_INVALID")
    for key in ("dry_run", "complete"):
        if not isinstance(fields[key], bool):
            raise NonForcedRestartCommandAdapterError("RESTART_COMMAND_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
