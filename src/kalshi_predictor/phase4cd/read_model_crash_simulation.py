from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SIMULATION_SCHEMA_VERSION = "phase4fs-read-model-crash-simulation-v1"
CrashPoint = Literal[
    "BEFORE_TEMP_WRITE",
    "AFTER_PARTIAL_TEMP_WRITE",
    "AFTER_TEMP_FSYNC",
    "AFTER_REPLACE",
]


class CrashSimulationError(ValueError):
    """Stable fail-closed crash-simulation error."""


@dataclass(frozen=True)
class RecoveryState:
    status: str
    current_sequence: int | None
    current_hash: str | None
    temporary_state: str


def initialize_workspace(root: Path) -> None:
    if not root.name.startswith("phase4fs-"):
        raise CrashSimulationError("SIMULATION_DIRECTORY_NAME_INVALID")
    root.mkdir(parents=True, exist_ok=False)
    (root / ".phase4fs-simulation").write_text(SIMULATION_SCHEMA_VERSION, encoding="utf-8")


def build_simulated_artifact(*, sequence: int, data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise CrashSimulationError("SEQUENCE_INVALID")
    if not isinstance(data, dict):
        raise CrashSimulationError("DATA_INVALID")
    payload: dict[str, Any] = {
        "schema_version": SIMULATION_SCHEMA_VERSION,
        "sequence": sequence,
        "data": data,
    }
    payload["artifact_hash"] = _hash(payload)
    return payload


def simulate_publication(
    root: Path,
    artifact: Any,
    *,
    crash_point: CrashPoint,
    max_artifact_bytes: int,
) -> RecoveryState:
    _require_workspace(root)
    _validate_artifact(artifact)
    if max_artifact_bytes <= 0:
        raise CrashSimulationError("SIZE_BOUND_INVALID")
    encoded = _encode(artifact)
    if len(encoded) > max_artifact_bytes:
        raise CrashSimulationError("ARTIFACT_SIZE_EXCEEDED")
    current = root / "current.json"
    temporary = root / ".current.json.candidate"
    if crash_point == "BEFORE_TEMP_WRITE":
        return inspect_workspace(root, max_artifact_bytes=max_artifact_bytes)
    if crash_point == "AFTER_PARTIAL_TEMP_WRITE":
        temporary.write_bytes(encoded[: max(1, len(encoded) // 2)])
        return inspect_workspace(root, max_artifact_bytes=max_artifact_bytes)
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    if crash_point == "AFTER_TEMP_FSYNC":
        return inspect_workspace(root, max_artifact_bytes=max_artifact_bytes)
    if crash_point != "AFTER_REPLACE":
        raise CrashSimulationError("CRASH_POINT_INVALID")
    _replace_atomically(temporary, current)
    return inspect_workspace(root, max_artifact_bytes=max_artifact_bytes)


def _replace_atomically(temporary: Path, current: Path) -> None:
    """Keep the old artifact intact while Windows readers release their handles."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.replace(temporary, current)
            return
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in {5, 32}
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.01)


def inspect_workspace(root: Path, *, max_artifact_bytes: int) -> RecoveryState:
    _require_workspace(root)
    if max_artifact_bytes <= 0:
        raise CrashSimulationError("SIZE_BOUND_INVALID")
    current = root / "current.json"
    temporary = root / ".current.json.candidate"
    current_artifact = _load_if_valid(current, max_artifact_bytes=max_artifact_bytes)
    temporary_state = "ABSENT"
    if temporary.exists():
        temporary_artifact = _load_if_valid(
            temporary,
            max_artifact_bytes=max_artifact_bytes,
        )
        temporary_state = "VALID" if temporary_artifact is not None else "INVALID"
    if current.exists() and current_artifact is None:
        raise CrashSimulationError("CURRENT_ARTIFACT_INVALID")
    if current_artifact is None:
        return RecoveryState(
            status="NO_CURRENT_ARTIFACT",
            current_sequence=None,
            current_hash=None,
            temporary_state=temporary_state,
        )
    return RecoveryState(
        status="CURRENT_ARTIFACT_VALID",
        current_sequence=current_artifact["sequence"],
        current_hash=current_artifact["artifact_hash"],
        temporary_state=temporary_state,
    )


def _load_if_valid(path: Path, *, max_artifact_bytes: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_size == 0 or path.stat().st_size > max_artifact_bytes:
            return None
        payload = json.loads(path.read_bytes())
        _validate_artifact(payload)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, CrashSimulationError):
        return None
    return payload


def _require_workspace(root: Path) -> None:
    if not root.name.startswith("phase4fs-"):
        raise CrashSimulationError("SIMULATION_DIRECTORY_NAME_INVALID")
    marker = root / ".phase4fs-simulation"
    try:
        value = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise CrashSimulationError("SIMULATION_MARKER_MISSING") from exc
    if value != SIMULATION_SCHEMA_VERSION:
        raise CrashSimulationError("SIMULATION_MARKER_INVALID")


def _validate_artifact(payload: Any) -> None:
    required = {"schema_version", "sequence", "data", "artifact_hash"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise CrashSimulationError("ARTIFACT_FIELDS_INVALID")
    if payload["schema_version"] != SIMULATION_SCHEMA_VERSION:
        raise CrashSimulationError("ARTIFACT_SCHEMA_UNSUPPORTED")
    if not isinstance(payload["sequence"], int) or isinstance(payload["sequence"], bool):
        raise CrashSimulationError("SEQUENCE_INVALID")
    if payload["sequence"] < 0 or not isinstance(payload["data"], dict):
        raise CrashSimulationError("ARTIFACT_VALUE_INVALID")
    unhashed = {key: value for key, value in payload.items() if key != "artifact_hash"}
    if payload["artifact_hash"] != _hash(unhashed):
        raise CrashSimulationError("ARTIFACT_HASH_MISMATCH")


def _encode(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
