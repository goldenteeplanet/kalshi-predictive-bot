from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kalshi_predictor.workstation.append_only_incident_writer import (
    AppendOnlyIncidentWriterError,
    append_incident_journal_entry,
    validate_incident_append_receipt,
)
from kalshi_predictor.workstation.incident_journal_schema import (
    GENESIS_ENTRY_HASH,
    make_incident_journal_entry,
)


def test_two_entries_append_with_exact_chain_and_receipts(tmp_path: Path) -> None:
    journal = tmp_path / "incidents.jsonl"
    first = _entry()
    receipt1 = append_incident_journal_entry(journal, first, allowed_root=tmp_path, dry_run=False)
    second = _entry(sequence=2, previous=first.entry_hash, event="RECOVERY_ATTEMPTED")
    receipt2 = append_incident_journal_entry(journal, second, allowed_root=tmp_path, dry_run=False)
    validate_incident_append_receipt(receipt1)
    validate_incident_append_receipt(receipt2)
    assert receipt1.entries_before == 0 and receipt1.entries_after == 1
    assert receipt2.entries_before == 1 and receipt2.entries_after == 2
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2
    assert receipt2.service_control_authorized is False


def test_dry_run_validates_without_creating_or_changing_file(tmp_path: Path) -> None:
    journal = tmp_path / "incidents.jsonl"
    receipt = append_incident_journal_entry(journal, _entry(), allowed_root=tmp_path)
    assert receipt.dry_run is True
    assert receipt.entries_before == receipt.entries_after == 0
    assert not journal.exists()
    append_incident_journal_entry(journal, _entry(), allowed_root=tmp_path, dry_run=False)
    before = journal.read_bytes()
    second = _entry(sequence=2, previous=_entry().entry_hash)
    append_incident_journal_entry(journal, second, allowed_root=tmp_path, dry_run=True)
    assert journal.read_bytes() == before


def test_path_escape_relative_suffix_and_directory_targets_are_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.jsonl"
    with pytest.raises(AppendOnlyIncidentWriterError, match="JOURNAL_PATH_OUTSIDE_ALLOWED_ROOT"):
        append_incident_journal_entry(outside, _entry(), allowed_root=tmp_path)
    with pytest.raises(AppendOnlyIncidentWriterError, match="JOURNAL_PATH_NOT_ABSOLUTE"):
        append_incident_journal_entry(Path("relative.jsonl"), _entry(), allowed_root=tmp_path)
    with pytest.raises(AppendOnlyIncidentWriterError, match="JOURNAL_SUFFIX_INVALID"):
        append_incident_journal_entry(tmp_path / "incidents.log", _entry(), allowed_root=tmp_path)


def test_entry_file_line_and_numeric_bounds_fail_closed(tmp_path: Path) -> None:
    journal = tmp_path / "incidents.jsonl"
    encoded_size = len(_canonical_line(_entry()))
    append_incident_journal_entry(
        journal,
        _entry(),
        allowed_root=tmp_path,
        dry_run=False,
        max_file_bytes=encoded_size,
    )
    second = _entry(sequence=2, previous=_entry().entry_hash)
    with pytest.raises(AppendOnlyIncidentWriterError, match="JOURNAL_FILE_BOUND_EXCEEDED"):
        append_incident_journal_entry(
            journal, second, allowed_root=tmp_path, max_file_bytes=encoded_size
        )
    with pytest.raises(AppendOnlyIncidentWriterError, match="JOURNAL_LINE_BOUND_EXCEEDED"):
        append_incident_journal_entry(
            tmp_path / "other.jsonl", _entry(), allowed_root=tmp_path, max_line_bytes=1
        )
    with pytest.raises(AppendOnlyIncidentWriterError, match="WRITER_BOUND_INVALID"):
        append_incident_journal_entry(journal, second, allowed_root=tmp_path, max_entries=True)


def test_sequence_chain_corrupt_and_partial_existing_journal_fail_closed(tmp_path: Path) -> None:
    journal = tmp_path / "incidents.jsonl"
    with pytest.raises(AppendOnlyIncidentWriterError, match="APPEND_SEQUENCE_INVALID"):
        append_incident_journal_entry(
            journal, _entry(sequence=2, previous="a" * 64), allowed_root=tmp_path
        )
    append_incident_journal_entry(journal, _entry(), allowed_root=tmp_path, dry_run=False)
    with pytest.raises(AppendOnlyIncidentWriterError, match="APPEND_CHAIN_INVALID"):
        append_incident_journal_entry(
            journal,
            _entry(sequence=2, previous="b" * 64),
            allowed_root=tmp_path,
        )
    journal.write_bytes(journal.read_bytes()[:-1])
    with pytest.raises(AppendOnlyIncidentWriterError, match="JOURNAL_TRAILING_RECORD_INCOMPLETE"):
        append_incident_journal_entry(
            journal,
            _entry(sequence=2, previous=_entry().entry_hash),
            allowed_root=tmp_path,
        )


def test_existing_record_and_receipt_tampering_fail_closed(tmp_path: Path) -> None:
    journal = tmp_path / "incidents.jsonl"
    receipt = append_incident_journal_entry(journal, _entry(), allowed_root=tmp_path, dry_run=False)
    journal.write_bytes(journal.read_bytes().replace(b"DETECTED", b"ALTERED_"))
    with pytest.raises(AppendOnlyIncidentWriterError, match="JOURNAL_RECORD_INVALID"):
        append_incident_journal_entry(
            journal,
            _entry(sequence=2, previous=_entry().entry_hash),
            allowed_root=tmp_path,
        )
    with pytest.raises(AppendOnlyIncidentWriterError, match="RECEIPT_HASH_MISMATCH"):
        validate_incident_append_receipt(replace(receipt, receipt_hash="0" * 64))
    with pytest.raises(AppendOnlyIncidentWriterError, match="RECEIPT_SAFETY_BOUNDARY_INVALID"):
        validate_incident_append_receipt(replace(receipt, host_restart_authorized=True))


def test_writer_has_no_database_service_notification_or_restart_surface() -> None:
    names = set(append_incident_journal_entry.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "commit",
            "connect",
            "execute",
            "restart",
            "shutdown",
            "start",
            "stop",
            "systemctl",
            "toast",
        }
    )


def _entry(*, sequence=1, previous=GENESIS_ENTRY_HASH, event="DETECTED"):
    return make_incident_journal_entry(
        sequence=sequence,
        incident_id="incident-1",
        observed_at_epoch_seconds=100 + sequence,
        event_type=event,
        severity="WARNING",
        summary="WSL liveness evidence unavailable",
        evidence_hash="a" * 64,
        source_identity_hash="b" * 64,
        previous_entry_hash=previous,
        complete=True,
    )


def _canonical_line(entry) -> bytes:
    from kalshi_predictor.workstation.incident_journal_schema import (
        canonicalize_incident_journal_entry,
    )

    return canonicalize_incident_journal_entry(entry) + b"\n"
