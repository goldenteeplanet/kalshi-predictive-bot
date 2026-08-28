from __future__ import annotations

import copy

from scripts.local.phase4lb_evidence_chain import create_receipt, validate_ledger


def _receipt(phase: str, commit: str, parent: str, when: str, previous=None):
    return create_receipt(
        phase_id=phase,
        commit=commit,
        parent=parent,
        tree="c" * 40,
        owned_paths=[f"docs/{phase.lower()}.md", f"scripts/{phase.lower()}.py"],
        staged_proof_sha256="d" * 64,
        ancestry_binding_sha256="e" * 64,
        tests_passed=7,
        tests_skipped=0,
        completed_at=when,
        previous_receipt_sha256=previous,
    )


def _chain():
    first = _receipt("4KX", "a" * 40, "0" * 40, "2026-08-28T20:00:00Z")
    second = _receipt("4KY", "b" * 40, "a" * 40, "2026-08-28T20:01:00Z", first["receipt_sha256"])
    return [first, second]


def test_valid_chain_passes_deterministically() -> None:
    chain = _chain()
    assert validate_ledger(chain) == validate_ledger(copy.deepcopy(chain))
    assert validate_ledger(chain)["verdict"] == "PASS"


def test_receipt_tampering_refuses() -> None:
    chain = _chain()
    chain[0]["tree"] = "f" * 40
    assert "RECEIPT_0:HASH_MISMATCH" in validate_ledger(chain)["errors"]


def test_broken_previous_hash_and_parent_refuse() -> None:
    chain = _chain()
    chain[1]["previous_receipt_sha256"] = "0" * 64
    chain[1]["parent"] = "9" * 40
    errors = validate_ledger(chain)["errors"]
    assert "RECEIPT_1:PREVIOUS_HASH_MISMATCH" in errors
    assert "RECEIPT_1:PARENT_CHAIN_MISMATCH" in errors


def test_duplicate_phase_and_commit_refuse() -> None:
    chain = _chain()
    chain[1]["phase_id"] = "4KX"
    chain[1]["commit"] = chain[0]["commit"]
    errors = validate_ledger(chain)["errors"]
    assert "RECEIPT_1:DUPLICATE_PHASE_ID" in errors
    assert "RECEIPT_1:DUPLICATE_COMMIT" in errors


def test_noncontiguous_phase_and_timestamp_refuse() -> None:
    chain = _chain()
    chain[1]["phase_id"] = "4KZ"
    chain[1]["completed_at"] = chain[0]["completed_at"]
    errors = validate_ledger(chain)["errors"]
    assert "RECEIPT_1:NON_CONTIGUOUS_PHASE" in errors
    assert "RECEIPT_1:NON_MONOTONIC_TIMESTAMP" in errors


def test_unsafe_paths_and_failed_safety_refuse() -> None:
    chain = _chain()
    chain[1]["owned_paths"] = ["../escape"]
    chain[1]["safety_verdict"] = "REFUSE"
    errors = validate_ledger(chain)["errors"]
    assert "RECEIPT_1:UNSAFE_PATH" in errors
    assert "RECEIPT_1:SAFETY_NOT_PASSING" in errors


def test_failed_or_empty_tests_refuse() -> None:
    chain = _chain()
    chain[1]["test_summary"] = {"passed": 0, "failed": 1, "skipped": 0}
    assert "RECEIPT_1:TESTS_NOT_PASSING" in validate_ledger(chain)["errors"]


def test_malformed_ledger_refuses() -> None:
    result = validate_ledger({"not": "a list"})
    assert result["verdict"] == "REFUSE"
    assert "LEDGER_NOT_A_LIST" in result["errors"]
