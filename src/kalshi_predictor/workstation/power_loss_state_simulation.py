from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

SIMULATION_SCHEMA_VERSION = "phase4jn-power-loss-state-simulation-v1"
EXPECTED_STATES = {
    "BEFORE_APPEND": (False, False, False),
    "DURING_APPEND": (True, False, False),
    "AFTER_APPEND_BEFORE_FSYNC": (True, True, False),
    "AFTER_FSYNC": (True, True, True),
}
SimulationStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]
RecoveryDisposition = Literal[
    "NO_INTENT", "REFUSE_CORRUPT", "REFUSE_NOT_DURABLE", "RECONCILE_DURABLE"
]


class PowerLossStateSimulationError(ValueError):
    """Stable fail-closed power-loss state simulation error."""


@dataclass(frozen=True)
class PowerLossFixture:
    simulation_id_hash: str
    sandbox_manifest_hash: str
    loss_boundary: str
    record_bytes_present: bool
    record_complete: bool
    fsync_completed: bool
    fixture_only: bool
    complete: bool
    fixture_hash: str


@dataclass(frozen=True)
class PowerLossSimulationResult:
    status: SimulationStatus
    reasons: tuple[str, ...]
    loss_boundary: str
    fixture_hash: str
    expected_state_hash: str
    observed_state_hash: str
    recovery_disposition: RecoveryDisposition
    state_trusted: bool
    automatic_replay_permitted: bool
    result_hash: str
    read_only: bool = True
    fixture_only: bool = True
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_power_loss_fixture(**fields: Any) -> PowerLossFixture:
    _validate_fields(fields)
    return PowerLossFixture(**fields, fixture_hash=_hash(fields))


def simulate_power_loss_state(fixture: Any) -> PowerLossSimulationResult:
    item = _validated_fixture(fixture)
    expected = EXPECTED_STATES.get(item.loss_boundary)
    observed = (item.record_bytes_present, item.record_complete, item.fsync_completed)
    if not item.record_bytes_present:
        disposition: RecoveryDisposition = "NO_INTENT"
    elif not item.record_complete:
        disposition = "REFUSE_CORRUPT"
    elif not item.fsync_completed:
        disposition = "REFUSE_NOT_DURABLE"
    else:
        disposition = "RECONCILE_DURABLE"
    if expected is None:
        status: SimulationStatus = "TAMPERED"
        reasons = [f"POWER_LOSS_BOUNDARY_UNKNOWN:{item.loss_boundary}"]
        expected_payload: Any = "UNKNOWN"
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["POWER_LOSS_FIXTURE_INCOMPLETE"]
        expected_payload = list(expected)
    elif not item.fixture_only:
        status = "TAMPERED"
        reasons = ["POWER_LOSS_NON_FIXTURE_REFUSED"]
        expected_payload = list(expected)
    elif observed != expected:
        status = "FAIL"
        reasons = ["POWER_LOSS_STATE_MISMATCH"]
        expected_payload = list(expected)
    else:
        status = "PASS"
        reasons = []
        expected_payload = list(expected)
    trusted = disposition in {"NO_INTENT", "RECONCILE_DURABLE"} and status == "PASS"
    unsigned = {
        "schema_version": SIMULATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "loss_boundary": item.loss_boundary,
        "fixture_hash": item.fixture_hash,
        "expected_state_hash": _hash(expected_payload),
        "observed_state_hash": _hash(list(observed)),
        "recovery_disposition": disposition,
        "state_trusted": trusted,
        "automatic_replay_permitted": False,
        "read_only": True,
        "fixture_only": True,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return PowerLossSimulationResult(
        status=status,
        reasons=tuple(reasons),
        loss_boundary=item.loss_boundary,
        fixture_hash=item.fixture_hash,
        expected_state_hash=unsigned["expected_state_hash"],
        observed_state_hash=unsigned["observed_state_hash"],
        recovery_disposition=disposition,
        state_trusted=trusted,
        automatic_replay_permitted=False,
        result_hash=_hash(unsigned),
    )


def validate_power_loss_simulation_result(value: Any) -> None:
    if not isinstance(value, PowerLossSimulationResult):
        raise PowerLossStateSimulationError("POWER_LOSS_RESULT_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.fixture_only is not True
        or value.automatic_replay_permitted is not False
        or any(
            (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
        )
    ):
        raise PowerLossStateSimulationError("POWER_LOSS_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = SIMULATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise PowerLossStateSimulationError("POWER_LOSS_RESULT_HASH_MISMATCH")


def _validated_fixture(value: Any) -> PowerLossFixture:
    if not isinstance(value, PowerLossFixture):
        raise PowerLossStateSimulationError("POWER_LOSS_FIXTURE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("fixture_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise PowerLossStateSimulationError("POWER_LOSS_FIXTURE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "simulation_id_hash",
        "sandbox_manifest_hash",
        "loss_boundary",
        "record_bytes_present",
        "record_complete",
        "fsync_completed",
        "fixture_only",
        "complete",
    }
    if set(fields) != required:
        raise PowerLossStateSimulationError("POWER_LOSS_FIXTURE_FIELD_INVALID")
    for key in ("simulation_id_hash", "sandbox_manifest_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise PowerLossStateSimulationError("POWER_LOSS_FIXTURE_FIELD_INVALID")
    if (
        not isinstance(fields["loss_boundary"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["loss_boundary"]) is None
    ):
        raise PowerLossStateSimulationError("POWER_LOSS_FIXTURE_FIELD_INVALID")
    for key in (
        "record_bytes_present",
        "record_complete",
        "fsync_completed",
        "fixture_only",
        "complete",
    ):
        if not isinstance(fields[key], bool):
            raise PowerLossStateSimulationError("POWER_LOSS_FIXTURE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
