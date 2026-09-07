from dataclasses import replace
from pathlib import Path

import pytest

from kalshi_predictor.workstation.supervisor_exclusion_lock import (
    SupervisorExclusionLockError,
    acquire_supervisor_exclusion_lock,
    make_supervisor_lock_request,
    validate_supervisor_lock_receipt,
)


def _request(**overrides):
    fields = dict(
        lock_id_hash="1" * 64,
        owner_identity_hash="2" * 64,
        host_identity_hash="3" * 64,
        acquired_at_epoch=1_000,
        complete=True,
    )
    fields.update(overrides)
    return make_supervisor_lock_request(**fields)


def test_default_dry_run_is_deterministic_and_non_mutating(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.lock"
    first = acquire_supervisor_exclusion_lock(path, _request(), allowed_root=tmp_path)
    assert first == acquire_supervisor_exclusion_lock(path, _request(), allowed_root=tmp_path)
    assert first.status == "DRY_RUN_READY" and not first.lock_acquired and not path.exists()
    assert not first.lock_steal_permitted and not first.restart_authorized
    validate_supervisor_lock_receipt(first)


def test_atomic_create_acquires_once_and_second_owner_is_held(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.lock"
    first = acquire_supervisor_exclusion_lock(
        path, _request(), allowed_root=tmp_path, dry_run=False
    )
    second = acquire_supervisor_exclusion_lock(
        path, _request(owner_identity_hash="4" * 64), allowed_root=tmp_path, dry_run=False
    )
    assert first.status == "ACQUIRED" and first.lock_acquired
    assert second.status == "HELD" and not second.lock_acquired
    assert path.exists()


def test_incomplete_existing_corrupt_and_overbound_state_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.lock"
    assert (
        acquire_supervisor_exclusion_lock(
            path, _request(complete=False), allowed_root=tmp_path
        ).status
        == "INCOMPLETE"
    )
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(SupervisorExclusionLockError, match="EXISTING_CORRUPT"):
        acquire_supervisor_exclusion_lock(path, _request(), allowed_root=tmp_path)
    with pytest.raises(SupervisorExclusionLockError, match="BOUND_EXCEEDED"):
        acquire_supervisor_exclusion_lock(path, _request(), allowed_root=tmp_path, max_lock_bytes=1)


def test_relative_escape_suffix_and_malformed_request_fail_closed(tmp_path: Path) -> None:
    for path in (Path("relative.lock"), tmp_path.parent / "outside.lock", tmp_path / "bad.txt"):
        with pytest.raises(SupervisorExclusionLockError, match="PATH_INVALID"):
            acquire_supervisor_exclusion_lock(path, _request(), allowed_root=tmp_path)
    with pytest.raises(SupervisorExclusionLockError, match="FIELD_INVALID"):
        _request(owner_identity_hash="bad")


def test_request_receipt_and_authority_tampering_fail_closed(tmp_path: Path) -> None:
    request = _request()
    with pytest.raises(SupervisorExclusionLockError, match="REQUEST_HASH_MISMATCH"):
        acquire_supervisor_exclusion_lock(
            tmp_path / "s.lock", replace(request, complete=False), allowed_root=tmp_path
        )
    receipt = acquire_supervisor_exclusion_lock(tmp_path / "s.lock", request, allowed_root=tmp_path)
    with pytest.raises(SupervisorExclusionLockError, match="RECEIPT_HASH_MISMATCH"):
        validate_supervisor_lock_receipt(replace(receipt, lock_record_hash="f" * 64))
    with pytest.raises(SupervisorExclusionLockError, match="SAFETY_BOUNDARY"):
        validate_supervisor_lock_receipt(replace(receipt, lock_steal_permitted=True))


def test_lock_has_no_delete_service_database_or_restart_surface() -> None:
    forbidden = {
        "unlink",
        "remove",
        "rmdir",
        "connect",
        "execute",
        "Popen",
        "subprocess",
        "system",
        "spawn",
        "shutdown",
    }
    assert forbidden.isdisjoint(acquire_supervisor_exclusion_lock.__code__.co_names)
