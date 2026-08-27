from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .incident_journal_schema import (
    GENESIS_ENTRY_HASH,
    IncidentJournalEntry,
    canonicalize_incident_journal_entry,
    validate_incident_journal_entry,
)

RECEIPT_SCHEMA_VERSION = "phase4hm-append-only-incident-writer-receipt-v1"


class AppendOnlyIncidentWriterError(ValueError):
    """Stable fail-closed append-only incident writer error."""


@dataclass(frozen=True)
class IncidentAppendReceipt:
    journal_path_hash: str
    entry_hash: str
    entry_sequence: int
    entries_before: int
    entries_after: int
    bytes_before: int
    bytes_after: int
    journal_hash_after: str
    dry_run: bool
    receipt_hash: str
    schema_version: str = RECEIPT_SCHEMA_VERSION
    append_only: bool = True
    local_only: bool = True
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def append_incident_journal_entry(
    journal_path: Path,
    entry: Any,
    *,
    allowed_root: Path,
    dry_run: bool = True,
    max_entries: int = 10_000,
    max_file_bytes: int = 8 * 1024 * 1024,
    max_line_bytes: int = 8 * 1024,
) -> IncidentAppendReceipt:
    for value in (max_entries, max_file_bytes, max_line_bytes):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AppendOnlyIncidentWriterError("WRITER_BOUND_INVALID")
    if not isinstance(dry_run, bool):
        raise AppendOnlyIncidentWriterError("DRY_RUN_INVALID")
    validate_incident_journal_entry(entry)
    target = _validated_target(journal_path, allowed_root)
    existing_bytes = _read_bounded_journal(target, max_file_bytes=max_file_bytes)
    existing_entries = _parse_and_validate_chain(existing_bytes, max_entries=max_entries)
    _validate_next_entry(existing_entries, entry)
    encoded_entry = canonicalize_incident_journal_entry(entry) + b"\n"
    if len(encoded_entry) > max_line_bytes:
        raise AppendOnlyIncidentWriterError("JOURNAL_LINE_BOUND_EXCEEDED")
    if len(existing_entries) + 1 > max_entries:
        raise AppendOnlyIncidentWriterError("JOURNAL_ENTRY_BOUND_EXCEEDED")
    after_bytes = existing_bytes + encoded_entry
    if len(after_bytes) > max_file_bytes:
        raise AppendOnlyIncidentWriterError("JOURNAL_FILE_BOUND_EXCEEDED")

    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_symlink():
            raise AppendOnlyIncidentWriterError("JOURNAL_SYMLINK_REFUSED")
        descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            written = os.write(descriptor, encoded_entry)
            if written != len(encoded_entry):
                raise AppendOnlyIncidentWriterError("JOURNAL_APPEND_INCOMPLETE")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    entries_after = len(existing_entries) + (0 if dry_run else 1)
    bytes_after = len(existing_bytes) + (0 if dry_run else len(encoded_entry))
    journal_after = existing_bytes if dry_run else after_bytes
    unsigned = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "journal_path_hash": _hash(str(target)),
        "entry_hash": entry.entry_hash,
        "entry_sequence": entry.sequence,
        "entries_before": len(existing_entries),
        "entries_after": entries_after,
        "bytes_before": len(existing_bytes),
        "bytes_after": bytes_after,
        "journal_hash_after": _hash_bytes(journal_after),
        "dry_run": dry_run,
        "append_only": True,
        "local_only": True,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return IncidentAppendReceipt(
        journal_path_hash=unsigned["journal_path_hash"],
        entry_hash=entry.entry_hash,
        entry_sequence=entry.sequence,
        entries_before=len(existing_entries),
        entries_after=entries_after,
        bytes_before=len(existing_bytes),
        bytes_after=bytes_after,
        journal_hash_after=unsigned["journal_hash_after"],
        dry_run=dry_run,
        receipt_hash=_hash(unsigned),
    )


def validate_incident_append_receipt(receipt: Any) -> None:
    if not isinstance(receipt, IncidentAppendReceipt):
        raise AppendOnlyIncidentWriterError("RECEIPT_TYPE_INVALID")
    if receipt.schema_version != RECEIPT_SCHEMA_VERSION:
        raise AppendOnlyIncidentWriterError("RECEIPT_SCHEMA_INVALID")
    if receipt.append_only is not True or receipt.local_only is not True or any(
        (
            receipt.recovery_authorized,
            receipt.service_control_authorized,
            receipt.host_restart_authorized,
            receipt.execution_authorized,
        )
    ):
        raise AppendOnlyIncidentWriterError("RECEIPT_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(receipt)
    unsigned.pop("receipt_hash")
    if receipt.receipt_hash != _hash(unsigned):
        raise AppendOnlyIncidentWriterError("RECEIPT_HASH_MISMATCH")


def _validated_target(journal_path: Any, allowed_root: Any) -> Path:
    if not isinstance(journal_path, Path) or not isinstance(allowed_root, Path):
        raise AppendOnlyIncidentWriterError("JOURNAL_PATH_TYPE_INVALID")
    if not journal_path.is_absolute() or not allowed_root.is_absolute():
        raise AppendOnlyIncidentWriterError("JOURNAL_PATH_NOT_ABSOLUTE")
    root = allowed_root.resolve(strict=False)
    target = journal_path.resolve(strict=False)
    if target == root or root not in target.parents:
        raise AppendOnlyIncidentWriterError("JOURNAL_PATH_OUTSIDE_ALLOWED_ROOT")
    if target.suffix != ".jsonl":
        raise AppendOnlyIncidentWriterError("JOURNAL_SUFFIX_INVALID")
    if target.exists() and target.is_symlink():
        raise AppendOnlyIncidentWriterError("JOURNAL_SYMLINK_REFUSED")
    return target


def _read_bounded_journal(target: Path, *, max_file_bytes: int) -> bytes:
    if not target.exists():
        return b""
    size = target.stat().st_size
    if size > max_file_bytes:
        raise AppendOnlyIncidentWriterError("JOURNAL_FILE_BOUND_EXCEEDED")
    data = target.read_bytes()
    if len(data) != size:
        raise AppendOnlyIncidentWriterError("JOURNAL_SIZE_CHANGED_DURING_READ")
    return data


def _parse_and_validate_chain(data: bytes, *, max_entries: int) -> list[IncidentJournalEntry]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise AppendOnlyIncidentWriterError("JOURNAL_TRAILING_RECORD_INCOMPLETE")
    lines = data.splitlines()
    if len(lines) > max_entries:
        raise AppendOnlyIncidentWriterError("JOURNAL_ENTRY_BOUND_EXCEEDED")
    entries = []
    for line in lines:
        try:
            payload = json.loads(line)
            entry = IncidentJournalEntry(**payload)
            validate_incident_journal_entry(entry)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AppendOnlyIncidentWriterError("JOURNAL_RECORD_INVALID") from exc
        expected_sequence = len(entries) + 1
        expected_previous = entries[-1].entry_hash if entries else GENESIS_ENTRY_HASH
        if entry.sequence != expected_sequence:
            raise AppendOnlyIncidentWriterError("JOURNAL_SEQUENCE_INVALID")
        if entry.previous_entry_hash != expected_previous:
            raise AppendOnlyIncidentWriterError("JOURNAL_CHAIN_INVALID")
        entries.append(entry)
    return entries


def _validate_next_entry(existing: list[IncidentJournalEntry], entry: IncidentJournalEntry) -> None:
    expected_sequence = len(existing) + 1
    expected_previous = existing[-1].entry_hash if existing else GENESIS_ENTRY_HASH
    if entry.sequence != expected_sequence:
        raise AppendOnlyIncidentWriterError("APPEND_SEQUENCE_INVALID")
    if entry.previous_entry_hash != expected_previous:
        raise AppendOnlyIncidentWriterError("APPEND_CHAIN_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
