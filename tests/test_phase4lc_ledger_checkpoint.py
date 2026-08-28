from __future__ import annotations

import copy

import pytest

from scripts.local.phase4lb_evidence_chain import create_receipt
from scripts.local.phase4lc_ledger_checkpoint import create_checkpoint, verify_recovery


def _receipt(phase, commit, parent, completed_at, previous=None):
    return create_receipt(
        phase_id=phase,
        commit=commit,
        parent=parent,
        tree="c" * 40,
        owned_paths=[f"docs/{phase.lower()}.md"],
        staged_proof_sha256="d" * 64,
        ancestry_binding_sha256="e" * 64,
        tests_passed=3,
        tests_skipped=0,
        completed_at=completed_at,
        previous_receipt_sha256=previous,
    )


def _ledger():
    first = _receipt("4KX", "a" * 40, "0" * 40, "2026-08-28T20:00:00Z")
    second = _receipt("4KY", "b" * 40, "a" * 40, "2026-08-28T20:01:00Z", first["receipt_sha256"])
    return [first, second]


def test_exact_recovery_passes_deterministically() -> None:
    ledger = _ledger()
    checkpoint = create_checkpoint(ledger, created_at="2026-08-28T20:02:00Z")
    first = verify_recovery(ledger, checkpoint)
    assert first == verify_recovery(copy.deepcopy(ledger), copy.deepcopy(checkpoint))
    assert first["verdict"] == "PASS"


def test_truncated_valid_prefix_refuses() -> None:
    ledger = _ledger()
    checkpoint = create_checkpoint(ledger, created_at="2026-08-28T20:02:00Z")
    errors = verify_recovery(ledger[:1], checkpoint)["errors"]
    assert "CHECKPOINT_RECEIPT_COUNT_MISMATCH" in errors
    assert "CHECKPOINT_TIP_RECEIPT_SHA256_MISMATCH" in errors


def test_extended_chain_refuses() -> None:
    ledger = _ledger()
    checkpoint = create_checkpoint(ledger, created_at="2026-08-28T20:02:00Z")
    third = _receipt(
        "4KZ",
        "f" * 40,
        "b" * 40,
        "2026-08-28T20:03:00Z",
        ledger[-1]["receipt_sha256"],
    )
    assert verify_recovery([*ledger, third], checkpoint)["verdict"] == "REFUSE"


def test_reordered_or_altered_ledger_refuses() -> None:
    ledger = _ledger()
    checkpoint = create_checkpoint(ledger, created_at="2026-08-28T20:02:00Z")
    assert "LEDGER_NOT_PASSING" in verify_recovery(list(reversed(ledger)), checkpoint)["errors"]
    ledger[0]["tree"] = "9" * 40
    assert "LEDGER_NOT_PASSING" in verify_recovery(ledger, checkpoint)["errors"]


def test_checkpoint_tampering_and_policy_refuse() -> None:
    ledger = _ledger()
    checkpoint = create_checkpoint(ledger, created_at="2026-08-28T20:02:00Z")
    checkpoint["recovery_policy"] = "ALLOW_PREFIX"
    errors = verify_recovery(ledger, checkpoint)["errors"]
    assert "UNSAFE_RECOVERY_POLICY" in errors
    assert "CHECKPOINT_HASH_MISMATCH" in errors


def test_malformed_checkpoint_refuses() -> None:
    result = verify_recovery(_ledger(), "bad")
    assert result["verdict"] == "REFUSE"
    assert "CHECKPOINT_NOT_AN_OBJECT" in result["errors"]


def test_creation_refuses_empty_or_failing_ledger() -> None:
    with pytest.raises(ValueError, match="non-empty passing"):
        create_checkpoint([], created_at="2026-08-28T20:02:00Z")
    bad = _ledger()
    bad[0]["receipt_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="non-empty passing"):
        create_checkpoint(bad, created_at="2026-08-28T20:02:00Z")


def test_creation_refuses_bad_or_early_timestamp() -> None:
    ledger = _ledger()
    with pytest.raises(ValueError, match="checkpoint time"):
        create_checkpoint(ledger, created_at="not-a-time")
    with pytest.raises(ValueError, match="checkpoint time"):
        create_checkpoint(ledger, created_at="2026-08-28T20:00:30Z")
