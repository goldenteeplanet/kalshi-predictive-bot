from dataclasses import replace
from pathlib import Path

import pytest

from kalshi_predictor.workstation.supervisor_heartbeat_artifact import (
    SupervisorHeartbeatArtifactError,
    make_supervisor_heartbeat,
    validate_supervisor_heartbeat_receipt,
    write_supervisor_heartbeat,
)


def _heartbeat(sequence=1, observed=1_000, **overrides):
    fields = dict(
        supervisor_instance_hash="1" * 64,
        boot_identity_hash="2" * 64,
        exclusion_lock_hash="3" * 64,
        configuration_hash="4" * 64,
        sequence=sequence,
        observed_at_epoch=observed,
        state="OBSERVING",
        complete=True,
    )
    fields.update(overrides)
    return make_supervisor_heartbeat(**fields)


def test_default_dry_run_is_deterministic_and_non_mutating(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    first = write_supervisor_heartbeat(path, _heartbeat(), allowed_root=tmp_path)
    assert first == write_supervisor_heartbeat(path, _heartbeat(), allowed_root=tmp_path)
    assert first.dry_run and first.bytes_written == 0 and not path.exists()
    assert not first.task_activation_authorized and not first.restart_authorized
    validate_supervisor_heartbeat_receipt(first)


def test_atomic_writes_require_monotonic_sequence_and_time(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    first = write_supervisor_heartbeat(path, _heartbeat(), allowed_root=tmp_path, dry_run=False)
    second = write_supervisor_heartbeat(
        path, _heartbeat(2, 1_001, state="ALERT_ONLY"), allowed_root=tmp_path, dry_run=False
    )
    assert first.sequence == 1 and second.sequence == 2 and second.bytes_written > 0
    assert "ALERT_ONLY" in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "heartbeat",
    [
        _heartbeat(1, 1_001),
        _heartbeat(2, 1_000),
        _heartbeat(2, 1_001, supervisor_instance_hash="5" * 64),
        _heartbeat(2, 1_001, boot_identity_hash="6" * 64),
    ],
)
def test_sequence_time_instance_and_boot_drift_fail_closed(tmp_path: Path, heartbeat) -> None:
    path = tmp_path / "heartbeat.json"
    write_supervisor_heartbeat(path, _heartbeat(), allowed_root=tmp_path, dry_run=False)
    with pytest.raises(SupervisorHeartbeatArtifactError):
        write_supervisor_heartbeat(path, heartbeat, allowed_root=tmp_path)


def test_incomplete_corrupt_bounds_and_path_escape_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(SupervisorHeartbeatArtifactError, match="INCOMPLETE"):
        write_supervisor_heartbeat(
            tmp_path / "h.json", _heartbeat(complete=False), allowed_root=tmp_path
        )
    path = tmp_path / "h.json"
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(SupervisorHeartbeatArtifactError, match="EXISTING_INVALID"):
        write_supervisor_heartbeat(path, _heartbeat(), allowed_root=tmp_path)
    with pytest.raises(SupervisorHeartbeatArtifactError, match="BOUND_EXCEEDED"):
        write_supervisor_heartbeat(
            tmp_path / "x.json", _heartbeat(), allowed_root=tmp_path, max_bytes=1
        )
    with pytest.raises(SupervisorHeartbeatArtifactError, match="PATH_INVALID"):
        write_supervisor_heartbeat(tmp_path.parent / "x.json", _heartbeat(), allowed_root=tmp_path)


def test_heartbeat_and_receipt_tampering_fail_closed(tmp_path: Path) -> None:
    heartbeat = _heartbeat()
    with pytest.raises(SupervisorHeartbeatArtifactError, match="HEARTBEAT_HASH_MISMATCH"):
        write_supervisor_heartbeat(
            tmp_path / "h.json", replace(heartbeat, state="DEGRADED"), allowed_root=tmp_path
        )
    receipt = write_supervisor_heartbeat(tmp_path / "h.json", heartbeat, allowed_root=tmp_path)
    with pytest.raises(SupervisorHeartbeatArtifactError, match="RECEIPT_HASH_MISMATCH"):
        validate_supervisor_heartbeat_receipt(replace(receipt, bytes_written=1))
    with pytest.raises(SupervisorHeartbeatArtifactError, match="SAFETY_BOUNDARY"):
        validate_supervisor_heartbeat_receipt(replace(receipt, restart_authorized=True))


def test_writer_has_no_database_task_service_or_restart_surface() -> None:
    forbidden = {
        "connect",
        "execute",
        "Popen",
        "subprocess",
        "system",
        "spawn",
        "schtasks",
        "shutdown",
    }
    assert forbidden.isdisjoint(write_supervisor_heartbeat.__code__.co_names)
