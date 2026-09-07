from dataclasses import replace

import pytest

from kalshi_predictor.workstation.disk_memory_evidence_capture import (
    DiskMemoryEvidenceCaptureError,
    capture_disk_memory_evidence,
    make_disk_memory_snapshot_evidence,
    validate_disk_memory_capture,
)


def _evidence(**overrides):
    fields = dict(
        snapshot_id_hash="a" * 64,
        mount_id_hash="b" * 64,
        observed_at_epoch_seconds=100,
        total_disk_bytes=1000,
        free_disk_bytes=250,
        total_inodes=100,
        free_inodes=50,
        total_memory_bytes=2000,
        available_memory_bytes=500,
        total_swap_bytes=1000,
        free_swap_bytes=100,
        oom_kill_count=0,
        complete=True,
    )
    fields.update(overrides)
    return make_disk_memory_snapshot_evidence(**fields)


def test_coherent_snapshot_is_deterministic_redacted_and_non_authorizing() -> None:
    first = capture_disk_memory_evidence(_evidence(), evaluated_at_epoch_seconds=100)
    second = capture_disk_memory_evidence(_evidence(), evaluated_at_epoch_seconds=100)
    assert first == second and first.status == "CAPTURED"
    assert first.free_disk_basis_points == 2500 and first.available_memory_basis_points == 2500
    assert first.mount_identity_redacted and not first.raw_output_retained
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_disk_memory_capture(first)


def test_exact_freshness_passes_then_stales() -> None:
    assert (
        capture_disk_memory_evidence(_evidence(), evaluated_at_epoch_seconds=220).status
        == "CAPTURED"
    )
    assert (
        capture_disk_memory_evidence(_evidence(), evaluated_at_epoch_seconds=221).status == "STALE"
    )


def test_zero_denominator_incomplete_future_and_contradictory_fail_closed() -> None:
    assert (
        capture_disk_memory_evidence(
            _evidence(total_disk_bytes=0, free_disk_bytes=0), evaluated_at_epoch_seconds=100
        ).status
        == "PARTIAL"
    )
    assert (
        capture_disk_memory_evidence(
            _evidence(complete=False), evaluated_at_epoch_seconds=100
        ).status
        == "PARTIAL"
    )
    assert (
        capture_disk_memory_evidence(
            _evidence(observed_at_epoch_seconds=101), evaluated_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )
    assert (
        capture_disk_memory_evidence(
            _evidence(free_disk_bytes=1001), evaluated_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )


def test_evidence_capture_safety_tampering_and_live_resource_surfaces_fail_closed() -> None:
    with pytest.raises(DiskMemoryEvidenceCaptureError, match="EVIDENCE_HASH_MISMATCH"):
        capture_disk_memory_evidence(
            replace(_evidence(), free_disk_bytes=1), evaluated_at_epoch_seconds=100
        )
    result = capture_disk_memory_evidence(_evidence(), evaluated_at_epoch_seconds=100)
    with pytest.raises(DiskMemoryEvidenceCaptureError, match="CAPTURE_HASH_MISMATCH"):
        validate_disk_memory_capture(replace(result, reasons=("FORGED",)))
    with pytest.raises(DiskMemoryEvidenceCaptureError, match="SAFETY_BOUNDARY"):
        validate_disk_memory_capture(replace(result, raw_output_retained=True))
    forbidden = {
        "statvfs",
        "disk_usage",
        "virtual_memory",
        "open",
        "run",
        "subprocess",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(capture_disk_memory_evidence.__code__.co_names)
