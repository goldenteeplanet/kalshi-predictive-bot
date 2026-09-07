from dataclasses import replace

import pytest
from kalshi_predictor.workstation.disk_exhaustion_classifier import (
    DiskExhaustionClassifierError,
    classify_disk_exhaustion,
    make_disk_capacity_evidence,
    validate_disk_exhaustion_decision,
)

GIB = 1_073_741_824


def _evidence(**overrides):
    fields = dict(
        probe_id_hash="a" * 64,
        mount_code="WSL_ROOT",
        observed_at_epoch_seconds=100,
        total_bytes=100 * GIB,
        free_bytes=10 * GIB,
        total_inodes=1000,
        free_inodes=100,
        read_only=False,
        complete=True,
    )
    fields.update(overrides)
    return make_disk_capacity_evidence(**fields)


def test_healthy_capacity_is_deterministic_and_non_authorizing() -> None:
    first = classify_disk_exhaustion(_evidence(), evaluated_at_epoch_seconds=100)
    second = classify_disk_exhaustion(_evidence(), evaluated_at_epoch_seconds=100)
    assert first == second and first.status == "HEALTHY" and first.disk_headroom_proven
    assert not any(
        (
            first.restart_eligible,
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_disk_exhaustion_decision(first)


def test_each_byte_percent_inode_and_read_only_boundary_fails_closed() -> None:
    assert (
        classify_disk_exhaustion(
            _evidence(free_bytes=GIB - 1), evaluated_at_epoch_seconds=100
        ).status
        == "EXHAUSTED"
    )
    assert (
        classify_disk_exhaustion(
            _evidence(free_bytes=5 * GIB - 1), evaluated_at_epoch_seconds=100
        ).status
        == "EXHAUSTED"
    )
    assert (
        classify_disk_exhaustion(_evidence(free_inodes=49), evaluated_at_epoch_seconds=100).status
        == "EXHAUSTED"
    )
    assert (
        classify_disk_exhaustion(_evidence(read_only=True), evaluated_at_epoch_seconds=100).status
        == "READ_ONLY"
    )


def test_exact_thresholds_are_healthy_and_never_restartable() -> None:
    result = classify_disk_exhaustion(
        _evidence(free_bytes=5 * GIB, free_inodes=50), evaluated_at_epoch_seconds=100
    )
    assert result.status == "HEALTHY" and result.restart_eligible is False


def test_zero_denominator_incomplete_future_stale_and_contradictory_fail_closed() -> None:
    assert (
        classify_disk_exhaustion(
            _evidence(total_bytes=0, free_bytes=0), evaluated_at_epoch_seconds=100
        ).status
        == "UNKNOWN"
    )
    assert (
        classify_disk_exhaustion(_evidence(complete=False), evaluated_at_epoch_seconds=100).status
        == "INCOMPLETE"
    )
    assert (
        classify_disk_exhaustion(
            _evidence(observed_at_epoch_seconds=101), evaluated_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )
    assert classify_disk_exhaustion(_evidence(), evaluated_at_epoch_seconds=221).status == "UNKNOWN"
    assert (
        classify_disk_exhaustion(
            _evidence(free_bytes=101 * GIB), evaluated_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )


def test_tampering_safety_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(DiskExhaustionClassifierError, match="EVIDENCE_HASH_MISMATCH"):
        classify_disk_exhaustion(
            replace(_evidence(), read_only=True), evaluated_at_epoch_seconds=100
        )
    result = classify_disk_exhaustion(_evidence(), evaluated_at_epoch_seconds=100)
    with pytest.raises(DiskExhaustionClassifierError, match="DECISION_HASH_MISMATCH"):
        validate_disk_exhaustion_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(DiskExhaustionClassifierError, match="SAFETY_BOUNDARY"):
        validate_disk_exhaustion_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "open",
        "statvfs",
        "disk_usage",
        "subprocess",
        "restart",
        "reboot",
        "shutdown",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(classify_disk_exhaustion.__code__.co_names)
