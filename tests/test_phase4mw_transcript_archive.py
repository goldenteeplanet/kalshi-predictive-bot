from __future__ import annotations

import copy

from scripts.local.phase4mw_transcript_archive import (
    append_event,
    compact_custody,
    create_entry,
    make_event,
    reconstruct_custody,
    simulate_hold_delete_race,
    validate_archive,
)


def _archive():
    return create_entry(
        transcript_sha256="a" * 64,
        policy_generation=3,
        created_at="2026-08-29T00:00:00Z",
        retention_class="AUDIT_30D",
        expires_at="2026-09-28T00:00:00Z",
        custodian="archive-a",
    )


def _validate(value, at="2026-09-29T00:00:00Z"):
    return validate_archive(value, evaluated_at=at)


def test_archive_identity_and_retention_are_deterministic() -> None:
    first = _archive()
    assert first == _archive()
    result = _validate(first)
    assert result["verdict"] == "PASS"
    assert result["state"] == "RETAINED"


def test_premature_delete_refuses_and_expired_delete_tombstones() -> None:
    archive = _archive()
    early = append_event(
        archive,
        "DELETE",
        "custodian",
        "2026-09-01T00:00:00Z",
        {"deleted_entry_sha256": archive["entry"]["entry_sha256"]},
    )
    assert "RETENTION_NOT_EXPIRED" in " ".join(_validate(early)["errors"])
    deleted = append_event(
        archive,
        "DELETE",
        "custodian",
        "2026-09-28T00:00:00Z",
        {"deleted_entry_sha256": archive["entry"]["entry_sha256"]},
    )
    result = _validate(deleted)
    assert result["verdict"] == "PASS"
    assert result["state"] == "TOMBSTONED"
    assert result["physical_deletion"] is False


def test_hold_blocks_deletion_until_exact_release() -> None:
    archive = append_event(
        _archive(), "HOLD", "legal", "2026-08-30T00:00:00Z", {"hold_id": "case-1"}
    )
    blocked = append_event(
        archive,
        "DELETE",
        "custodian",
        "2026-09-28T00:00:00Z",
        {"deleted_entry_sha256": archive["entry"]["entry_sha256"]},
    )
    assert "LEGAL_HOLD_ACTIVE" in " ".join(_validate(blocked)["errors"])
    released = append_event(
        archive, "RELEASE", "legal", "2026-09-29T00:00:00Z", {"hold_id": "case-1"}
    )
    deleted = append_event(
        released,
        "DELETE",
        "custodian",
        "2026-09-29T00:01:00Z",
        {"deleted_entry_sha256": archive["entry"]["entry_sha256"]},
    )
    assert _validate(deleted)["state"] == "TOMBSTONED"


def test_duplicate_tombstone_post_delete_mutation_and_clock_rollback_refuse() -> None:
    archive = _archive()
    deleted = append_event(
        archive,
        "DELETE",
        "custodian",
        "2026-09-28T00:00:00Z",
        {"deleted_entry_sha256": archive["entry"]["entry_sha256"]},
    )
    duplicate = append_event(
        deleted,
        "DELETE",
        "custodian",
        "2026-09-28T00:01:00Z",
        {"deleted_entry_sha256": archive["entry"]["entry_sha256"]},
    )
    assert "DUPLICATE_TOMBSTONE" in " ".join(_validate(duplicate)["errors"])
    transfer = append_event(deleted, "TRANSFER", "custodian", "2026-09-28T00:01:00Z", {})
    assert "POST_TOMBSTONE_MUTATION" in " ".join(_validate(transfer)["errors"])
    rollback = append_event(archive, "TRANSFER", "custodian", "2026-08-28T00:00:00Z", {})
    assert "TIME_INVALID_OR_ROLLBACK" in " ".join(_validate(rollback)["errors"])


def test_custody_and_tombstone_tampering_refuse() -> None:
    archive = append_event(_archive(), "TRANSFER", "archive-b", "2026-08-30T00:00:00Z", {})
    archive["events"][1]["actor"] = "attacker"
    assert "HASH_INVALID" in " ".join(_validate(archive)["errors"])
    archive = _archive()
    wrong = append_event(
        archive, "DELETE", "custodian", "2026-09-28T00:00:00Z", {"deleted_entry_sha256": "f" * 64}
    )
    assert "TOMBSTONE_BINDING_INVALID" in " ".join(_validate(wrong)["errors"])


def test_concurrent_hold_delete_race_linearizes_fail_closed() -> None:
    archive = _archive()
    hold = make_event(
        archive["events"], "HOLD", "legal", "2026-09-28T00:00:00Z", {"hold_id": "case-1"}
    )
    delete = make_event(
        archive["events"],
        "DELETE",
        "custodian",
        "2026-09-28T00:00:00Z",
        {"deleted_entry_sha256": archive["entry"]["entry_sha256"]},
    )
    result = simulate_hold_delete_race(archive, hold, delete, evaluated_at="2026-09-29T00:00:00Z")
    assert result["outcomes"] == ["ACCEPTED", "REFUSED"]
    assert result["accepted_count"] == 1


def test_compaction_and_independent_reconstruction_preserve_audit() -> None:
    archive = append_event(_archive(), "TRANSFER", "archive-b", "2026-08-30T00:00:00Z", {})
    archive = append_event(archive, "HOLD", "legal", "2026-08-31T00:00:00Z", {"hold_id": "case-1"})
    compacted = compact_custody(archive, retain_tail=1)
    restored = reconstruct_custody(
        compacted, archive["events"][:2], evaluated_at="2026-09-01T00:00:00Z"
    )
    assert restored["verdict"] == "PASS"
    assert restored["events"] == archive["events"]
    damaged = copy.deepcopy(archive["events"][:2])
    damaged[0]["actor"] = "attacker"
    assert (
        reconstruct_custody(compacted, damaged, evaluated_at="2026-09-01T00:00:00Z")["verdict"]
        == "REFUSE"
    )


def test_simulated_deletion_has_no_real_or_operational_capability() -> None:
    safety = _validate(_archive())["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
