from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

SIMULATION_SCHEMA_VERSION = "phase4jm-crash-boundary-simulation-v1"
BOUNDARY_EXPECTATIONS = {
    "BEFORE_INTENT_PERSIST": (False, False, 0),
    "AFTER_INTENT_PERSIST": (True, True, 0),
    "AFTER_WARNING_RECORDED": (True, True, 0),
    "BEFORE_MOCK_EXECUTION": (True, True, 0),
    "AFTER_MOCK_EXECUTION": (True, True, 1),
    "BEFORE_POST_BOOT_VERIFICATION": (True, True, 1),
}
SimulationStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]


class CrashBoundarySimulationError(ValueError):
    """Stable fail-closed crash-boundary simulation error."""


@dataclass(frozen=True)
class CrashBoundaryFixture:
    simulation_id_hash: str
    sandbox_manifest_hash: str
    boundary: str
    intent_persisted: bool
    post_boot_pending: bool
    mock_invocation_count: int
    fixture_only: bool
    complete: bool
    fixture_hash: str


@dataclass(frozen=True)
class CrashBoundarySimulationResult:
    status: SimulationStatus
    reasons: tuple[str, ...]
    boundary: str
    fixture_hash: str
    expected_state_hash: str
    observed_state_hash: str
    reconciliation_required: bool
    automatic_replay_permitted: bool
    result_hash: str
    read_only: bool = True
    fixture_only: bool = True
    restart_authorized: bool = False
    process_spawn_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_crash_boundary_fixture(**fields: Any) -> CrashBoundaryFixture:
    _validate_fields(fields)
    return CrashBoundaryFixture(**fields, fixture_hash=_hash(fields))


def simulate_crash_boundary(fixture: Any) -> CrashBoundarySimulationResult:
    item = _validated_fixture(fixture)
    expected = BOUNDARY_EXPECTATIONS.get(item.boundary)
    observed = (item.intent_persisted, item.post_boot_pending, item.mock_invocation_count)
    if expected is None:
        status: SimulationStatus = "TAMPERED"
        reasons = [f"CRASH_BOUNDARY_UNKNOWN:{item.boundary}"]
        expected_payload: Any = "UNKNOWN"
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["CRASH_BOUNDARY_FIXTURE_INCOMPLETE"]
        expected_payload = list(expected)
    elif not item.fixture_only:
        status = "TAMPERED"
        reasons = ["CRASH_BOUNDARY_NON_FIXTURE_REFUSED"]
        expected_payload = list(expected)
    elif observed != expected:
        status = "FAIL"
        reasons = ["CRASH_BOUNDARY_STATE_MISMATCH"]
        expected_payload = list(expected)
    else:
        status = "PASS"
        reasons = []
        expected_payload = list(expected)
    reconciliation = item.intent_persisted or item.mock_invocation_count > 0
    unsigned = {
        "schema_version": SIMULATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "boundary": item.boundary,
        "fixture_hash": item.fixture_hash,
        "expected_state_hash": _hash(expected_payload),
        "observed_state_hash": _hash(list(observed)),
        "reconciliation_required": reconciliation,
        "automatic_replay_permitted": False,
        "read_only": True,
        "fixture_only": True,
        "restart_authorized": False,
        "process_spawn_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return CrashBoundarySimulationResult(
        status=status,
        reasons=tuple(reasons),
        boundary=item.boundary,
        fixture_hash=item.fixture_hash,
        expected_state_hash=unsigned["expected_state_hash"],
        observed_state_hash=unsigned["observed_state_hash"],
        reconciliation_required=reconciliation,
        automatic_replay_permitted=False,
        result_hash=_hash(unsigned),
    )


def validate_crash_boundary_simulation_result(value: Any) -> None:
    if not isinstance(value, CrashBoundarySimulationResult):
        raise CrashBoundarySimulationError("CRASH_BOUNDARY_RESULT_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.fixture_only is not True
        or value.automatic_replay_permitted is not False
        or any(
            (
                value.restart_authorized,
                value.process_spawn_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise CrashBoundarySimulationError("CRASH_BOUNDARY_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = SIMULATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise CrashBoundarySimulationError("CRASH_BOUNDARY_RESULT_HASH_MISMATCH")


def _validated_fixture(value: Any) -> CrashBoundaryFixture:
    if not isinstance(value, CrashBoundaryFixture):
        raise CrashBoundarySimulationError("CRASH_BOUNDARY_FIXTURE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("fixture_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise CrashBoundarySimulationError("CRASH_BOUNDARY_FIXTURE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "simulation_id_hash",
        "sandbox_manifest_hash",
        "boundary",
        "intent_persisted",
        "post_boot_pending",
        "mock_invocation_count",
        "fixture_only",
        "complete",
    }
    if set(fields) != required:
        raise CrashBoundarySimulationError("CRASH_BOUNDARY_FIXTURE_FIELD_INVALID")
    for key in ("simulation_id_hash", "sandbox_manifest_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise CrashBoundarySimulationError("CRASH_BOUNDARY_FIXTURE_FIELD_INVALID")
    if (
        not isinstance(fields["boundary"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["boundary"]) is None
        or isinstance(fields["mock_invocation_count"], bool)
        or not isinstance(fields["mock_invocation_count"], int)
        or not 0 <= fields["mock_invocation_count"] <= 1
    ):
        raise CrashBoundarySimulationError("CRASH_BOUNDARY_FIXTURE_FIELD_INVALID")
    for key in ("intent_persisted", "post_boot_pending", "fixture_only", "complete"):
        if not isinstance(fields[key], bool):
            raise CrashBoundarySimulationError("CRASH_BOUNDARY_FIXTURE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
