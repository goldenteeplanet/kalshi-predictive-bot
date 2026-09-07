from dataclasses import replace
from pathlib import Path

import pytest
from kalshi_predictor.workstation.persistent_restart_intent_record import (
    GENESIS_RECORD_HASH,
    PersistentRestartIntentRecordError,
    append_restart_intent_record,
    make_restart_intent_record,
    validate_restart_intent_append_receipt,
)


def _record(sequence=1, previous=GENESIS_RECORD_HASH, **overrides):
    fields = dict(
        sequence=sequence,
        incident_id_hash="1" * 64,
        reason_hash="2" * 64,
        evidence_bundle_hash="3" * 64,
        eligibility_decision_hash="4" * 64,
        warning_decision_hash="5" * 64,
        cancellation_token_hash="6" * 64,
        created_at_epoch=1_000,
        warning_ends_at_epoch=1_300,
        attempt_counter=1,
        post_boot_verification_pending=True,
        complete=True,
        previous_record_hash=previous,
    )
    fields.update(overrides)
    return make_restart_intent_record(**fields)


def test_append_persists_complete_intent_chain_and_non_authorizing_receipt(tmp_path: Path) -> None:
    path = tmp_path / "restart" / "intents.jsonl"
    first = _record()
    receipt1 = append_restart_intent_record(
        path, first, allowed_windows_root=tmp_path, dry_run=False
    )
    second = _record(2, first.record_hash, attempt_counter=2)
    receipt2 = append_restart_intent_record(
        path, second, allowed_windows_root=tmp_path, dry_run=False
    )
    assert receipt1.records_after == 1 and receipt2.records_after == 2
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    assert receipt2.windows_side_required and not receipt2.restart_authorized
    validate_restart_intent_append_receipt(receipt2)


def test_default_dry_run_validates_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "intents.jsonl"
    receipt = append_restart_intent_record(path, _record(), allowed_windows_root=tmp_path)
    assert receipt.dry_run and receipt.records_before == receipt.records_after == 0
    assert not path.exists()


def test_path_escape_relative_and_wrong_suffix_fail_closed(tmp_path: Path) -> None:
    for path in (tmp_path.parent / "outside.jsonl", Path("relative.jsonl"), tmp_path / "x.txt"):
        with pytest.raises(PersistentRestartIntentRecordError, match="PATH_INVALID"):
            append_restart_intent_record(path, _record(), allowed_windows_root=tmp_path)


def test_incomplete_or_missing_post_boot_marker_is_refused(tmp_path: Path) -> None:
    for record in (_record(complete=False), _record(post_boot_verification_pending=False)):
        with pytest.raises(PersistentRestartIntentRecordError, match="SAFETY_MARKER_INVALID"):
            append_restart_intent_record(
                tmp_path / "i.jsonl", record, allowed_windows_root=tmp_path
            )


def test_chain_partial_corrupt_and_bounds_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "i.jsonl"
    with pytest.raises(PersistentRestartIntentRecordError, match="SEQUENCE_INVALID"):
        append_restart_intent_record(path, _record(2), allowed_windows_root=tmp_path)
    append_restart_intent_record(path, _record(), allowed_windows_root=tmp_path, dry_run=False)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(PersistentRestartIntentRecordError, match="TRAILING_RECORD_INCOMPLETE"):
        append_restart_intent_record(
            path, _record(2, _record().record_hash), allowed_windows_root=tmp_path
        )
    with pytest.raises(PersistentRestartIntentRecordError, match="BOUND_INVALID"):
        append_restart_intent_record(
            tmp_path / "x.jsonl", _record(), allowed_windows_root=tmp_path, max_records=0
        )


def test_record_and_receipt_tampering_fail_closed(tmp_path: Path) -> None:
    record = _record()
    with pytest.raises(PersistentRestartIntentRecordError, match="RECORD_HASH_MISMATCH"):
        append_restart_intent_record(
            tmp_path / "i.jsonl", replace(record, attempt_counter=2), allowed_windows_root=tmp_path
        )
    receipt = append_restart_intent_record(
        tmp_path / "i.jsonl", record, allowed_windows_root=tmp_path
    )
    with pytest.raises(PersistentRestartIntentRecordError, match="RECEIPT_HASH_MISMATCH"):
        validate_restart_intent_append_receipt(replace(receipt, bytes_after=1))
    with pytest.raises(PersistentRestartIntentRecordError, match="RECEIPT_SAFETY_INVALID"):
        validate_restart_intent_append_receipt(replace(receipt, restart_authorized=True))


def test_writer_has_no_database_service_notification_or_restart_surface() -> None:
    forbidden = {"Popen", "connect", "execute", "restart", "shutdown", "systemctl", "toast"}
    assert forbidden.isdisjoint(append_restart_intent_record.__code__.co_names)
