from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

RECORD_SCHEMA_VERSION = "phase4jd-persistent-restart-intent-record-v1"
RECEIPT_SCHEMA_VERSION = "phase4jd-persistent-restart-intent-receipt-v1"
GENESIS_RECORD_HASH = "0" * 64


class PersistentRestartIntentRecordError(ValueError):
    """Stable fail-closed persistent restart-intent record error."""


@dataclass(frozen=True)
class RestartIntentRecord:
    sequence: int
    incident_id_hash: str
    reason_hash: str
    evidence_bundle_hash: str
    eligibility_decision_hash: str
    warning_decision_hash: str
    cancellation_token_hash: str
    created_at_epoch: int
    warning_ends_at_epoch: int
    attempt_counter: int
    post_boot_verification_pending: bool
    complete: bool
    previous_record_hash: str
    record_hash: str


@dataclass(frozen=True)
class RestartIntentAppendReceipt:
    record_hash: str
    path_hash: str
    records_before: int
    records_after: int
    bytes_before: int
    bytes_after: int
    file_hash_after: str
    dry_run: bool
    receipt_hash: str
    schema_version: str = RECEIPT_SCHEMA_VERSION
    append_only: bool = True
    windows_side_required: bool = True
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_intent_record(**fields: Any) -> RestartIntentRecord:
    _validate_record_fields(fields)
    unsigned = {"schema_version": RECORD_SCHEMA_VERSION, **fields}
    return RestartIntentRecord(**fields, record_hash=_hash(unsigned))


def append_restart_intent_record(
    path: Path,
    record: Any,
    *,
    allowed_windows_root: Path,
    dry_run: bool = True,
    max_records: int = 1_000,
    max_file_bytes: int = 2 * 1024 * 1024,
) -> RestartIntentAppendReceipt:
    if not isinstance(dry_run, bool):
        raise PersistentRestartIntentRecordError("RESTART_INTENT_DRY_RUN_INVALID")
    for bound in (max_records, max_file_bytes):
        if isinstance(bound, bool) or not isinstance(bound, int) or bound <= 0:
            raise PersistentRestartIntentRecordError("RESTART_INTENT_BOUND_INVALID")
    item = _validated_record(record)
    target = _validated_target(path, allowed_windows_root)
    before = _read_bounded(target, max_file_bytes)
    records = _parse_chain(before, max_records)
    expected_sequence = len(records) + 1
    expected_previous = records[-1].record_hash if records else GENESIS_RECORD_HASH
    if item.sequence != expected_sequence:
        raise PersistentRestartIntentRecordError("RESTART_INTENT_SEQUENCE_INVALID")
    if item.previous_record_hash != expected_previous:
        raise PersistentRestartIntentRecordError("RESTART_INTENT_CHAIN_INVALID")
    if not item.complete or not item.post_boot_verification_pending:
        raise PersistentRestartIntentRecordError("RESTART_INTENT_SAFETY_MARKER_INVALID")
    encoded = _canonical(item) + b"\n"
    after = before + encoded
    if len(records) + 1 > max_records:
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_BOUND_EXCEEDED")
    if len(after) > max_file_bytes:
        raise PersistentRestartIntentRecordError("RESTART_INTENT_FILE_BOUND_EXCEEDED")
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_symlink():
            raise PersistentRestartIntentRecordError("RESTART_INTENT_SYMLINK_REFUSED")
        descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise PersistentRestartIntentRecordError("RESTART_INTENT_APPEND_INCOMPLETE")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    persisted = not dry_run
    unsigned = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "record_hash": item.record_hash,
        "path_hash": _hash(str(target)),
        "records_before": len(records),
        "records_after": len(records) + (1 if persisted else 0),
        "bytes_before": len(before),
        "bytes_after": len(after) if persisted else len(before),
        "file_hash_after": _hash_bytes(after if persisted else before),
        "dry_run": dry_run,
        "append_only": True,
        "windows_side_required": True,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartIntentAppendReceipt(
        record_hash=item.record_hash,
        path_hash=unsigned["path_hash"],
        records_before=len(records),
        records_after=unsigned["records_after"],
        bytes_before=len(before),
        bytes_after=unsigned["bytes_after"],
        file_hash_after=unsigned["file_hash_after"],
        dry_run=dry_run,
        receipt_hash=_hash(unsigned),
    )


def validate_restart_intent_append_receipt(value: Any) -> None:
    if not isinstance(value, RestartIntentAppendReceipt):
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECEIPT_TYPE_INVALID")
    if (
        value.schema_version != RECEIPT_SCHEMA_VERSION
        or value.append_only is not True
        or value.windows_side_required is not True
        or any(
            (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
        )
    ):
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECEIPT_SAFETY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("receipt_hash")
    if value.receipt_hash != _hash(unsigned):
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECEIPT_HASH_MISMATCH")


def _validated_record(value: Any) -> RestartIntentRecord:
    if not isinstance(value, RestartIntentRecord):
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("record_hash")
    _validate_record_fields(unsigned)
    if supplied != _hash({"schema_version": RECORD_SCHEMA_VERSION, **unsigned}):
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_HASH_MISMATCH")
    return value


def _validate_record_fields(fields: dict[str, Any]) -> None:
    required = {
        "sequence",
        "incident_id_hash",
        "reason_hash",
        "evidence_bundle_hash",
        "eligibility_decision_hash",
        "warning_decision_hash",
        "cancellation_token_hash",
        "created_at_epoch",
        "warning_ends_at_epoch",
        "attempt_counter",
        "post_boot_verification_pending",
        "complete",
        "previous_record_hash",
    }
    if set(fields) != required:
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_FIELD_INVALID")
    for key in (
        "incident_id_hash",
        "reason_hash",
        "evidence_bundle_hash",
        "eligibility_decision_hash",
        "warning_decision_hash",
        "cancellation_token_hash",
        "previous_record_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_FIELD_INVALID")
    for key in ("sequence", "created_at_epoch", "warning_ends_at_epoch", "attempt_counter"):
        if isinstance(fields[key], bool) or not isinstance(fields[key], int) or fields[key] < 0:
            raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_FIELD_INVALID")
    if (
        fields["sequence"] < 1
        or fields["attempt_counter"] < 1
        or fields["warning_ends_at_epoch"] <= fields["created_at_epoch"]
    ):
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_FIELD_INVALID")
    for key in ("post_boot_verification_pending", "complete"):
        if not isinstance(fields[key], bool):
            raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_FIELD_INVALID")


def _validated_target(path: Any, root_path: Any) -> Path:
    if (
        not isinstance(path, Path)
        or not isinstance(root_path, Path)
        or not path.is_absolute()
        or not root_path.is_absolute()
    ):
        raise PersistentRestartIntentRecordError("RESTART_INTENT_PATH_INVALID")
    root = root_path.resolve(strict=False)
    target = path.resolve(strict=False)
    if target == root or root not in target.parents or target.suffix != ".jsonl":
        raise PersistentRestartIntentRecordError("RESTART_INTENT_PATH_INVALID")
    if target.exists() and target.is_symlink():
        raise PersistentRestartIntentRecordError("RESTART_INTENT_SYMLINK_REFUSED")
    return target


def _read_bounded(path: Path, maximum: int) -> bytes:
    if not path.exists():
        return b""
    if path.stat().st_size > maximum:
        raise PersistentRestartIntentRecordError("RESTART_INTENT_FILE_BOUND_EXCEEDED")
    return path.read_bytes()


def _parse_chain(data: bytes, maximum: int) -> list[RestartIntentRecord]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise PersistentRestartIntentRecordError("RESTART_INTENT_TRAILING_RECORD_INCOMPLETE")
    lines = data.splitlines()
    if len(lines) > maximum:
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_BOUND_EXCEEDED")
    records = []
    try:
        for line in lines:
            record = RestartIntentRecord(**json.loads(line))
            _validated_record(record)
            expected_previous = records[-1].record_hash if records else GENESIS_RECORD_HASH
            if (
                record.sequence != len(records) + 1
                or record.previous_record_hash != expected_previous
            ):
                raise PersistentRestartIntentRecordError("RESTART_INTENT_EXISTING_CHAIN_INVALID")
            records.append(record)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PersistentRestartIntentRecordError("RESTART_INTENT_RECORD_INVALID") from exc
    return records


def _canonical(record: RestartIntentRecord) -> bytes:
    return json.dumps(asdict(record), sort_keys=True, separators=(",", ":")).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
