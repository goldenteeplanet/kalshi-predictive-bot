from __future__ import annotations

import copy

import pytest

from scripts.local.phase4oh_freeze_durability import (
    append_event,
    create_checkpoint,
    restore_state,
)


def _freeze(journal=None):
    return append_event(
        journal or [],
        event_type="FREEZE",
        evidence_sha256="a" * 64,
        evidence_verdict="PASS",
    )


def _recover(journal):
    return append_event(
        journal,
        event_type="RECOVER",
        evidence_sha256="b" * 64,
        evidence_verdict="PASS",
    )


def test_restart_reconstructs_freeze_and_blocks_capabilities() -> None:
    journal = _freeze()
    checkpoint = create_checkpoint(journal)
    result = restore_state(journal, checkpoint)
    assert result["integrity_verdict"] == "PASS"
    assert result["state"] == "FROZEN"
    assert result["capabilities_allowed"] is False


def test_verified_recovery_restores_state_and_stale_checkpoint_cannot_override_new_freeze() -> None:
    journal = _freeze()
    journal = _recover(journal)
    stale_checkpoint = create_checkpoint(journal)
    journal = append_event(
        journal,
        event_type="FREEZE",
        evidence_sha256="c" * 64,
        evidence_verdict="PASS",
    )
    result = restore_state(journal, stale_checkpoint)
    assert result["integrity_verdict"] == "PASS"
    assert result["state"] == "FROZEN"
    assert result["capabilities_allowed"] is False


def test_missing_truncated_reordered_duplicated_and_corrupted_records_fail_closed() -> None:
    journal = _recover(_freeze())
    cases = [
        ([], "JOURNAL_MISSING"),
        ([journal[1]], "JOURNAL_SEQUENCE_GAP_OR_REORDER"),
        (list(reversed(journal)), "JOURNAL_SEQUENCE_GAP_OR_REORDER"),
        ([journal[0], journal[0]], "JOURNAL_DUPLICATE"),
    ]
    corrupted = copy.deepcopy(journal)
    corrupted[1]["evidence_sha256"] = "f" * 64
    cases.append((corrupted, "JOURNAL_HASH_MISMATCH"))
    for candidate, expected in cases:
        result = restore_state(candidate, checkpoint=None)
        assert result["integrity_verdict"] == "REFUSE"
        assert result["state"] == "FROZEN"
        assert result["capabilities_allowed"] is False
        assert expected in result["errors"]


def test_invalid_recovery_evidence_never_restores_capabilities() -> None:
    journal = _freeze()
    journal = append_event(
        journal,
        event_type="RECOVER",
        evidence_sha256="b" * 64,
        evidence_verdict="REFUSE",
    )
    result = restore_state(journal, checkpoint=None)
    assert "RECOVERY_EVIDENCE_INVALID" in result["errors"]
    assert result["state"] == "FROZEN"
    assert result["capabilities_allowed"] is False


def test_checkpoint_tamper_wrong_head_and_future_sequence_fail_closed() -> None:
    journal = _freeze()
    checkpoint = create_checkpoint(journal)
    variants = []
    tampered = copy.deepcopy(checkpoint)
    tampered["state"] = "RECOVERED"
    variants.append((tampered, "CHECKPOINT_HASH_MISMATCH"))
    wrong_head = copy.deepcopy(checkpoint)
    wrong_head["head_sha256"] = "f" * 64
    variants.append((wrong_head, "CHECKPOINT_HEAD_MISMATCH"))
    future = copy.deepcopy(checkpoint)
    future["sequence"] = 2
    variants.append((future, "CHECKPOINT_SEQUENCE_INVALID"))
    for candidate, expected in variants:
        result = restore_state(journal, candidate)
        assert result["state"] == "FROZEN"
        assert expected in result["errors"]


def test_checkpoint_refuses_invalid_journal() -> None:
    with pytest.raises(ValueError, match="invalid journal"):
        create_checkpoint([])


def test_replay_is_deterministic_input_preserving_and_execution_free() -> None:
    journal = _recover(_freeze())
    checkpoint = create_checkpoint(journal)
    original = copy.deepcopy((journal, checkpoint))
    first = restore_state(journal, checkpoint)
    second = restore_state(journal, checkpoint)
    assert first == second
    assert (journal, checkpoint) == original
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
