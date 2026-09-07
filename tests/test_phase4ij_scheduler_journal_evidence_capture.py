from dataclasses import replace

import pytest

from kalshi_predictor.workstation.scheduler_journal_evidence_capture import (
    SchedulerJournalEvidenceCaptureError,
    capture_scheduler_journal_evidence,
    make_scheduler_journal_entry_evidence,
    validate_scheduler_journal_capture,
)


def _entry(n, **overrides):
    fields = dict(
        cursor_hash=str(n) * 64,
        unit_id_hash="a" * 64,
        occurred_at_epoch_seconds=100 + n,
        priority=6,
        message_hash="b" * 64,
        byte_count=100,
        complete=True,
    )
    fields.update(overrides)
    return make_scheduler_journal_entry_evidence(**fields)


def test_capture_is_deterministic_redacted_and_non_authorizing() -> None:
    first = capture_scheduler_journal_evidence(
        [_entry(2), _entry(1, priority=4)],
        window_start_epoch_seconds=100,
        window_end_epoch_seconds=200,
    )
    second = capture_scheduler_journal_evidence(
        [_entry(1, priority=4), _entry(2)],
        window_start_epoch_seconds=100,
        window_end_epoch_seconds=200,
    )
    assert first == second and first.status == "CAPTURED" and first.warning_or_higher_count == 1
    assert first.messages_redacted and not first.raw_output_retained
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_scheduler_journal_capture(first)


def test_exact_entry_byte_and_time_bounds_pass_then_excess_refuses() -> None:
    records = [
        _entry(1, occurred_at_epoch_seconds=100, byte_count=50),
        _entry(2, occurred_at_epoch_seconds=200, byte_count=50),
    ]
    result = capture_scheduler_journal_evidence(
        records,
        window_start_epoch_seconds=100,
        window_end_epoch_seconds=200,
        max_entries=2,
        max_total_bytes=100,
    )
    assert result.status == "CAPTURED"
    with pytest.raises(SchedulerJournalEvidenceCaptureError, match="ENTRY_BOUND_EXCEEDED"):
        capture_scheduler_journal_evidence(
            records, window_start_epoch_seconds=100, window_end_epoch_seconds=200, max_entries=1
        )
    assert (
        capture_scheduler_journal_evidence(
            [_entry(1, byte_count=101)],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
            max_total_bytes=100,
        ).status
        == "REFUSED"
    )
    assert (
        capture_scheduler_journal_evidence(
            [_entry(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=100
        ).status
        == "REFUSED"
    )


def test_incomplete_duplicate_and_tampered_entries_fail_closed() -> None:
    assert (
        capture_scheduler_journal_evidence(
            [_entry(1, complete=False)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        ).status
        == "PARTIAL"
    )
    assert (
        capture_scheduler_journal_evidence(
            [_entry(1), _entry(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        ).status
        == "TAMPERED"
    )
    with pytest.raises(SchedulerJournalEvidenceCaptureError, match="ENTRY_HASH_MISMATCH"):
        capture_scheduler_journal_evidence(
            [replace(_entry(1), priority=0)],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
        )


def test_capture_safety_tampering_and_journalctl_surfaces_fail_closed() -> None:
    result = capture_scheduler_journal_evidence(
        [_entry(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
    )
    with pytest.raises(SchedulerJournalEvidenceCaptureError, match="CAPTURE_HASH_MISMATCH"):
        validate_scheduler_journal_capture(replace(result, reasons=("FORGED",)))
    with pytest.raises(SchedulerJournalEvidenceCaptureError, match="SAFETY_BOUNDARY"):
        validate_scheduler_journal_capture(replace(result, raw_output_retained=True))
    forbidden = {
        "journalctl",
        "open",
        "run",
        "popen",
        "subprocess",
        "systemctl",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(capture_scheduler_journal_evidence.__code__.co_names)
