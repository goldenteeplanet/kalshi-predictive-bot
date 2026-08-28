from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

CAPTURE_SCHEMA_VERSION = "phase4ig-process-tree-evidence-capture-v1"
ProcessState = Literal["RUNNING", "SLEEPING", "STOPPED", "ZOMBIE", "UNKNOWN"]
CaptureStatus = Literal["CAPTURED", "PARTIAL", "TAMPERED", "REFUSED"]


class ProcessTreeEvidenceCaptureError(ValueError):
    """Stable fail-closed process-tree evidence capture error."""


@dataclass(frozen=True)
class ProcessNodeEvidence:
    process_id_hash: str
    parent_process_id_hash: str | None
    executable_hash: str
    state: ProcessState
    started_at_epoch_seconds: int
    complete: bool
    node_hash: str


@dataclass(frozen=True)
class ProcessTreeCapture:
    status: CaptureStatus
    reasons: tuple[str, ...]
    captured_at_epoch_seconds: int
    node_count: int
    root_count: int
    maximum_observed_depth: int
    node_hashes: tuple[str, ...]
    tree_hash: str
    read_only: bool = True
    identifiers_redacted: bool = True
    command_lines_retained: bool = False
    environment_retained: bool = False
    process_tree_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_process_node_evidence(**fields: Any) -> ProcessNodeEvidence:
    _validate_fields(fields)
    return ProcessNodeEvidence(**fields, node_hash=_hash(fields))


def capture_process_tree_evidence(
    nodes: Sequence[Any],
    *,
    captured_at_epoch_seconds: int,
    max_nodes: int = 128,
    max_depth: int = 16,
) -> ProcessTreeCapture:
    for value in (captured_at_epoch_seconds, max_nodes, max_depth):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ProcessTreeEvidenceCaptureError("PROCESS_TREE_BOUND_INVALID")
    if isinstance(nodes, (str, bytes)) or len(nodes) > max_nodes:
        raise ProcessTreeEvidenceCaptureError("PROCESS_TREE_NODE_BOUND_EXCEEDED")
    records = [_validated_node(item) for item in nodes]
    records.sort(key=lambda item: item.process_id_hash)
    by_id = {item.process_id_hash: item for item in records}
    duplicate = len(by_id) != len(records)
    missing_parent = any(
        item.parent_process_id_hash is not None and item.parent_process_id_hash not in by_id
        for item in records
    )
    future = any(item.started_at_epoch_seconds > captured_at_epoch_seconds for item in records)
    depths: dict[str, int] = {}
    cycle = False
    for item in records:
        seen: set[str] = set()
        current = item
        depth = 0
        while (
            current.parent_process_id_hash is not None and current.parent_process_id_hash in by_id
        ):
            if current.process_id_hash in seen:
                cycle = True
                break
            seen.add(current.process_id_hash)
            depth += 1
            current = by_id[current.parent_process_id_hash]
        depths[item.process_id_hash] = depth
    observed_depth = max(depths.values(), default=0)
    if duplicate or cycle:
        status: CaptureStatus = "TAMPERED"
        reasons = ["PROCESS_TREE_DUPLICATE_OR_CYCLE"]
    elif future:
        status = "TAMPERED"
        reasons = ["PROCESS_TREE_NODE_FROM_FUTURE"]
    elif missing_parent or any(not item.complete for item in records):
        status = "PARTIAL"
        reasons = ["PROCESS_TREE_PARENT_OR_NODE_INCOMPLETE"]
    elif observed_depth > max_depth:
        status = "REFUSED"
        reasons = ["PROCESS_TREE_DEPTH_BOUND_EXCEEDED"]
    else:
        status = "CAPTURED"
        reasons = []
    complete = status == "CAPTURED"
    hashes = tuple(item.node_hash for item in records)
    root_count = sum(item.parent_process_id_hash is None for item in records)
    unsigned = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "captured_at_epoch_seconds": captured_at_epoch_seconds,
        "node_count": len(records),
        "root_count": root_count,
        "maximum_observed_depth": observed_depth,
        "node_hashes": list(hashes),
        "read_only": True,
        "identifiers_redacted": True,
        "command_lines_retained": False,
        "environment_retained": False,
        "process_tree_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return ProcessTreeCapture(
        status=status,
        reasons=tuple(reasons),
        captured_at_epoch_seconds=captured_at_epoch_seconds,
        node_count=len(records),
        root_count=root_count,
        maximum_observed_depth=observed_depth,
        node_hashes=hashes,
        tree_hash=_hash(unsigned),
        process_tree_complete=complete,
    )


def validate_process_tree_capture(value: Any) -> None:
    if not isinstance(value, ProcessTreeCapture):
        raise ProcessTreeEvidenceCaptureError("PROCESS_TREE_CAPTURE_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.identifiers_redacted is not True
        or value.command_lines_retained is not False
        or value.environment_retained is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise ProcessTreeEvidenceCaptureError("PROCESS_TREE_SAFETY_BOUNDARY_INVALID")
    if value.process_tree_complete != (value.status == "CAPTURED"):
        raise ProcessTreeEvidenceCaptureError("PROCESS_TREE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("tree_hash")
    unsigned["schema_version"] = CAPTURE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["node_hashes"] = list(unsigned["node_hashes"])
    if value.tree_hash != _hash(unsigned):
        raise ProcessTreeEvidenceCaptureError("PROCESS_TREE_CAPTURE_HASH_MISMATCH")


def _validated_node(value: Any) -> ProcessNodeEvidence:
    if not isinstance(value, ProcessNodeEvidence):
        raise ProcessTreeEvidenceCaptureError("PROCESS_NODE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("node_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise ProcessTreeEvidenceCaptureError("PROCESS_NODE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "process_id_hash",
        "parent_process_id_hash",
        "executable_hash",
        "state",
        "started_at_epoch_seconds",
        "complete",
    }
    if set(fields) != required:
        raise ProcessTreeEvidenceCaptureError("PROCESS_NODE_FIELD_INVALID")
    for key in ("process_id_hash", "executable_hash"):
        if not _is_hash(fields[key]):
            raise ProcessTreeEvidenceCaptureError("PROCESS_NODE_FIELD_INVALID")
    if fields["parent_process_id_hash"] is not None and not _is_hash(
        fields["parent_process_id_hash"]
    ):
        raise ProcessTreeEvidenceCaptureError("PROCESS_NODE_FIELD_INVALID")
    if fields["state"] not in {"RUNNING", "SLEEPING", "STOPPED", "ZOMBIE", "UNKNOWN"}:
        raise ProcessTreeEvidenceCaptureError("PROCESS_NODE_FIELD_INVALID")
    timestamp = fields["started_at_epoch_seconds"]
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
        or not isinstance(fields["complete"], bool)
    ):
        raise ProcessTreeEvidenceCaptureError("PROCESS_NODE_FIELD_INVALID")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
