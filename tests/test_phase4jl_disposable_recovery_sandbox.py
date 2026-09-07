from dataclasses import replace
from pathlib import Path

import pytest

from kalshi_predictor.workstation.disposable_recovery_sandbox import (
    DisposableRecoverySandboxError,
    make_recovery_sandbox_request,
    provision_disposable_recovery_sandbox,
    validate_recovery_sandbox_receipt,
)


def _request(**overrides):
    fields = dict(
        sandbox_id="recovery-fixture-1",
        production_identity_hash="1" * 64,
        fixture_identity_hash="2" * 64,
        source_evidence_hash="3" * 64,
        test_host=True,
        disposable=True,
        dry_run=True,
        complete=True,
    )
    fields.update(overrides)
    return make_recovery_sandbox_request(**fields)


def test_default_dry_run_is_deterministic_and_does_not_create_files(tmp_path: Path) -> None:
    first = provision_disposable_recovery_sandbox(tmp_path, _request(), allowed_root=tmp_path)
    second = provision_disposable_recovery_sandbox(tmp_path, _request(), allowed_root=tmp_path)
    assert first == second and first.status == "DRY_RUN_READY"
    assert first.production_isolated and not first.manifest_written
    assert not (tmp_path / "recovery-fixture-1").exists()
    assert not first.restart_authorized
    validate_recovery_sandbox_receipt(first)


def test_explicit_fixture_mode_writes_only_manifest_under_temporary_root(tmp_path: Path) -> None:
    receipt = provision_disposable_recovery_sandbox(
        tmp_path, _request(dry_run=False), allowed_root=tmp_path
    )
    sandbox = tmp_path / "recovery-fixture-1"
    assert receipt.status == "READY" and receipt.manifest_written
    assert [path.name for path in sandbox.iterdir()] == ["sandbox-manifest.json"]
    assert "production_identity_hash" in (sandbox / "sandbox-manifest.json").read_text()


@pytest.mark.parametrize(
    "overrides",
    [
        {"test_host": False},
        {"disposable": False},
    ],
)
def test_non_test_or_non_disposable_request_is_denied(tmp_path: Path, overrides) -> None:
    assert (
        provision_disposable_recovery_sandbox(
            tmp_path, _request(**overrides), allowed_root=tmp_path
        ).status
        == "DENIED"
    )


def test_production_identity_collision_and_incomplete_request_fail_closed(tmp_path: Path) -> None:
    collision = _request(fixture_identity_hash="1" * 64)
    assert (
        provision_disposable_recovery_sandbox(tmp_path, collision, allowed_root=tmp_path).status
        == "TAMPERED"
    )
    assert (
        provision_disposable_recovery_sandbox(
            tmp_path, _request(complete=False), allowed_root=tmp_path
        ).status
        == "INCOMPLETE"
    )


def test_existing_target_relative_path_and_escape_are_refused(tmp_path: Path) -> None:
    (tmp_path / "recovery-fixture-1").mkdir()
    assert (
        provision_disposable_recovery_sandbox(tmp_path, _request(), allowed_root=tmp_path).status
        == "DENIED"
    )
    with pytest.raises(DisposableRecoverySandboxError, match="PATH_INVALID"):
        provision_disposable_recovery_sandbox(Path("relative"), _request(), allowed_root=tmp_path)
    with pytest.raises(DisposableRecoverySandboxError, match="OUTSIDE_ALLOWED_ROOT"):
        provision_disposable_recovery_sandbox(tmp_path.parent, _request(), allowed_root=tmp_path)


def test_request_receipt_and_authority_tampering_fail_closed(tmp_path: Path) -> None:
    request = _request()
    with pytest.raises(DisposableRecoverySandboxError, match="REQUEST_HASH_MISMATCH"):
        provision_disposable_recovery_sandbox(
            tmp_path, replace(request, dry_run=False), allowed_root=tmp_path
        )
    receipt = provision_disposable_recovery_sandbox(tmp_path, request, allowed_root=tmp_path)
    with pytest.raises(DisposableRecoverySandboxError, match="RECEIPT_HASH_MISMATCH"):
        validate_recovery_sandbox_receipt(replace(receipt, manifest_hash="f" * 64))
    with pytest.raises(DisposableRecoverySandboxError, match="SAFETY_BOUNDARY"):
        validate_recovery_sandbox_receipt(replace(receipt, database_write_authorized=True))


def test_sandbox_has_no_database_service_or_restart_surface() -> None:
    forbidden = {
        "connect",
        "execute",
        "Popen",
        "subprocess",
        "system",
        "spawn",
        "socket",
        "shutdown",
    }
    assert forbidden.isdisjoint(provision_disposable_recovery_sandbox.__code__.co_names)
