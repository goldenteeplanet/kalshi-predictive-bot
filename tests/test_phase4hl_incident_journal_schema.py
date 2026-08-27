from __future__ import annotations

import json
from dataclasses import replace

import pytest

from kalshi_predictor.workstation.incident_journal_schema import (
    GENESIS_ENTRY_HASH,
    JOURNAL_SCHEMA_VERSION,
    MAX_INCIDENT_ID_CHARACTERS,
    MAX_SUMMARY_CHARACTERS,
    IncidentJournalSchemaError,
    canonicalize_incident_journal_entry,
    make_incident_journal_entry,
    validate_incident_journal_entry,
)


def test_valid_entry_is_deterministic_canonical_local_and_safe() -> None:
    first = _entry()
    second = _entry()
    validate_incident_journal_entry(first)
    assert first.entry_hash == second.entry_hash
    assert canonicalize_incident_journal_entry(first) == canonicalize_incident_journal_entry(second)
    payload = json.loads(canonicalize_incident_journal_entry(first))
    assert payload["schema_version"] == JOURNAL_SCHEMA_VERSION
    assert first.local_only is True
    assert first.host_restart_authorized is False


def test_all_lifecycle_events_and_severities_are_supported() -> None:
    events = (
        "DETECTED",
        "RECOVERY_ATTEMPTED",
        "RECOVERY_SUCCEEDED",
        "RECOVERY_FAILED",
        "RESTART_SCHEDULED",
        "RESTART_CANCELLED",
        "POST_BOOT_VERIFIED",
        "POST_BOOT_FAILED",
    )
    for index, event in enumerate(events, start=1):
        severity = ("INFO", "WARNING", "CRITICAL")[index % 3]
        validate_incident_journal_entry(_entry(event=event, severity=severity))


def test_exact_text_bounds_pass_and_excess_fails_closed() -> None:
    validate_incident_journal_entry(
        _entry(incident_id="i" * MAX_INCIDENT_ID_CHARACTERS, summary="s" * MAX_SUMMARY_CHARACTERS)
    )
    with pytest.raises(IncidentJournalSchemaError, match="ENTRY_FIELD_INVALID"):
        _entry(incident_id="i" * (MAX_INCIDENT_ID_CHARACTERS + 1))
    with pytest.raises(IncidentJournalSchemaError, match="ENTRY_FIELD_INVALID"):
        _entry(summary="s" * (MAX_SUMMARY_CHARACTERS + 1))
    with pytest.raises(IncidentJournalSchemaError, match="ENTRY_FIELD_INVALID"):
        _entry(summary="unsafe\nsecond-line")


def test_genesis_and_non_genesis_link_rules_fail_closed() -> None:
    validate_incident_journal_entry(_entry(sequence=1, previous=GENESIS_ENTRY_HASH))
    with pytest.raises(IncidentJournalSchemaError, match="GENESIS_LINK_INVALID"):
        _entry(sequence=1, previous="a" * 64)
    validate_incident_journal_entry(_entry(sequence=2, previous="a" * 64))
    with pytest.raises(IncidentJournalSchemaError, match="NON_GENESIS_LINK_INVALID"):
        _entry(sequence=2, previous=GENESIS_ENTRY_HASH)


def test_malformed_enum_hash_numeric_and_boolean_fields_fail_closed() -> None:
    for kwargs in (
        {"event": "UNKNOWN"},
        {"severity": "EMERGENCY"},
        {"evidence": "not-a-hash"},
        {"sequence": 0},
        {"complete": "yes"},
    ):
        with pytest.raises(IncidentJournalSchemaError):
            _entry(**kwargs)


def test_entry_content_hash_schema_and_safety_tampering_fail_closed() -> None:
    entry = _entry()
    with pytest.raises(IncidentJournalSchemaError, match="ENTRY_HASH_MISMATCH"):
        validate_incident_journal_entry(replace(entry, summary="changed"))
    with pytest.raises(IncidentJournalSchemaError, match="ENTRY_SCHEMA_VERSION_INVALID"):
        validate_incident_journal_entry(replace(entry, schema_version="other"))
    with pytest.raises(IncidentJournalSchemaError, match="ENTRY_SAFETY_BOUNDARY_INVALID"):
        validate_incident_journal_entry(replace(entry, recovery_authorized=True))


def test_schema_has_no_file_notification_control_or_mutation_surface() -> None:
    names = set(canonicalize_incident_journal_entry.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "commit",
            "connect",
            "execute",
            "open",
            "restart",
            "shutdown",
            "start",
            "stop",
            "systemctl",
            "toast",
            "write",
        }
    )


def _entry(
    *,
    sequence=1,
    incident_id="incident-1",
    event="DETECTED",
    severity="WARNING",
    summary="WSL liveness evidence unavailable",
    evidence="a" * 64,
    previous=GENESIS_ENTRY_HASH,
    complete=True,
):
    return make_incident_journal_entry(
        sequence=sequence,
        incident_id=incident_id,
        observed_at_epoch_seconds=100,
        event_type=event,
        severity=severity,
        summary=summary,
        evidence_hash=evidence,
        source_identity_hash="b" * 64,
        previous_entry_hash=previous,
        complete=complete,
    )
