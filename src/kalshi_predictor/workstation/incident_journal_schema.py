from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

JOURNAL_SCHEMA_VERSION = "phase4hl-local-incident-journal-entry-v1"
GENESIS_ENTRY_HASH = "0" * 64
MAX_INCIDENT_ID_CHARACTERS = 128
MAX_SUMMARY_CHARACTERS = 512
IncidentSeverity = Literal["INFO", "WARNING", "CRITICAL"]
IncidentEventType = Literal[
    "DETECTED",
    "RECOVERY_ATTEMPTED",
    "RECOVERY_SUCCEEDED",
    "RECOVERY_FAILED",
    "RESTART_SCHEDULED",
    "RESTART_CANCELLED",
    "POST_BOOT_VERIFIED",
    "POST_BOOT_FAILED",
]


class IncidentJournalSchemaError(ValueError):
    """Stable fail-closed incident-journal schema error."""


@dataclass(frozen=True)
class IncidentJournalEntry:
    sequence: int
    incident_id: str
    observed_at_epoch_seconds: int
    event_type: IncidentEventType
    severity: IncidentSeverity
    summary: str
    evidence_hash: str
    source_identity_hash: str
    previous_entry_hash: str
    complete: bool
    entry_hash: str
    schema_version: str = JOURNAL_SCHEMA_VERSION
    local_only: bool = True
    read_only_evidence: bool = True
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_incident_journal_entry(
    *,
    sequence: int,
    incident_id: str,
    observed_at_epoch_seconds: int,
    event_type: IncidentEventType,
    severity: IncidentSeverity,
    summary: str,
    evidence_hash: str,
    source_identity_hash: str,
    previous_entry_hash: str,
    complete: bool,
) -> IncidentJournalEntry:
    unsigned = {
        "sequence": sequence,
        "incident_id": incident_id,
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "event_type": event_type,
        "severity": severity,
        "summary": summary,
        "evidence_hash": evidence_hash,
        "source_identity_hash": source_identity_hash,
        "previous_entry_hash": previous_entry_hash,
        "complete": complete,
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "local_only": True,
        "read_only_evidence": True,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    _validate_entry_fields(unsigned)
    return IncidentJournalEntry(
        sequence=sequence,
        incident_id=incident_id,
        observed_at_epoch_seconds=observed_at_epoch_seconds,
        event_type=event_type,
        severity=severity,
        summary=summary,
        evidence_hash=evidence_hash,
        source_identity_hash=source_identity_hash,
        previous_entry_hash=previous_entry_hash,
        complete=complete,
        entry_hash=_hash(unsigned),
    )


def validate_incident_journal_entry(entry: Any) -> None:
    if not isinstance(entry, IncidentJournalEntry):
        raise IncidentJournalSchemaError("ENTRY_TYPE_INVALID")
    unsigned = asdict(entry)
    supplied_hash = unsigned.pop("entry_hash")
    _validate_entry_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise IncidentJournalSchemaError("ENTRY_HASH_MISMATCH")


def canonicalize_incident_journal_entry(entry: Any) -> bytes:
    validate_incident_journal_entry(entry)
    return json.dumps(asdict(entry), sort_keys=True, separators=(",", ":")).encode()


def _validate_entry_fields(payload: dict[str, Any]) -> None:
    sequence = payload["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise IncidentJournalSchemaError("ENTRY_FIELD_INVALID")
    observed_at = payload["observed_at_epoch_seconds"]
    if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < 0:
        raise IncidentJournalSchemaError("ENTRY_FIELD_INVALID")
    incident_id = payload["incident_id"]
    if (
        not isinstance(incident_id, str)
        or not incident_id.strip()
        or len(incident_id) > MAX_INCIDENT_ID_CHARACTERS
    ):
        raise IncidentJournalSchemaError("ENTRY_FIELD_INVALID")
    summary = payload["summary"]
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > MAX_SUMMARY_CHARACTERS
        or "\n" in summary
        or "\r" in summary
    ):
        raise IncidentJournalSchemaError("ENTRY_FIELD_INVALID")
    if payload["event_type"] not in {
        "DETECTED",
        "RECOVERY_ATTEMPTED",
        "RECOVERY_SUCCEEDED",
        "RECOVERY_FAILED",
        "RESTART_SCHEDULED",
        "RESTART_CANCELLED",
        "POST_BOOT_VERIFIED",
        "POST_BOOT_FAILED",
    }:
        raise IncidentJournalSchemaError("ENTRY_FIELD_INVALID")
    if payload["severity"] not in {"INFO", "WARNING", "CRITICAL"}:
        raise IncidentJournalSchemaError("ENTRY_FIELD_INVALID")
    for key in ("evidence_hash", "source_identity_hash", "previous_entry_hash"):
        if not _is_sha256(payload[key]):
            raise IncidentJournalSchemaError("ENTRY_FIELD_INVALID")
    if sequence == 1 and payload["previous_entry_hash"] != GENESIS_ENTRY_HASH:
        raise IncidentJournalSchemaError("GENESIS_LINK_INVALID")
    if sequence > 1 and payload["previous_entry_hash"] == GENESIS_ENTRY_HASH:
        raise IncidentJournalSchemaError("NON_GENESIS_LINK_INVALID")
    if not isinstance(payload["complete"], bool):
        raise IncidentJournalSchemaError("ENTRY_FIELD_INVALID")
    if payload["schema_version"] != JOURNAL_SCHEMA_VERSION:
        raise IncidentJournalSchemaError("ENTRY_SCHEMA_VERSION_INVALID")
    if payload["local_only"] is not True or payload["read_only_evidence"] is not True:
        raise IncidentJournalSchemaError("ENTRY_SAFETY_BOUNDARY_INVALID")
    if any(
        payload[key]
        for key in (
            "recovery_authorized",
            "service_control_authorized",
            "host_restart_authorized",
            "execution_authorized",
        )
    ):
        raise IncidentJournalSchemaError("ENTRY_SAFETY_BOUNDARY_INVALID")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
