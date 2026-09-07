from dataclasses import replace

import pytest
from kalshi_predictor.workstation.systemd_status_evidence_capture import (
    SystemdStatusEvidenceCaptureError,
    capture_systemd_status_evidence,
    make_systemd_unit_evidence,
    validate_systemd_status_capture,
)


def _unit(n, **overrides):
    fields = dict(
        unit_id_hash=str(n) * 64,
        scope="USER",
        load_state="LOADED",
        active_state="ACTIVE",
        sub_state_code="RUNNING",
        observed_at_epoch_seconds=100,
        complete=True,
    )
    fields.update(overrides)
    return make_systemd_unit_evidence(**fields)


def test_capture_is_deterministic_redacted_scoped_and_non_authorizing() -> None:
    records = [_unit(1), _unit(2, scope="SYSTEM", active_state="FAILED", sub_state_code="FAILED")]
    first = capture_systemd_status_evidence(list(reversed(records)), captured_at_epoch_seconds=100)
    second = capture_systemd_status_evidence(records, captured_at_epoch_seconds=100)
    assert first == second and first.status == "CAPTURED"
    assert first.user_scope_count == first.system_scope_count == first.failed_count == 1
    assert first.unit_names_redacted and not first.raw_output_retained
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_systemd_status_capture(first)


def test_exact_unit_bound_passes_then_excess_raises() -> None:
    records = [_unit(1), _unit(2)]
    assert (
        capture_systemd_status_evidence(records, captured_at_epoch_seconds=100, max_units=2).status
        == "CAPTURED"
    )
    with pytest.raises(SystemdStatusEvidenceCaptureError, match="BOUND_EXCEEDED"):
        capture_systemd_status_evidence(records, captured_at_epoch_seconds=100, max_units=1)


def test_empty_incomplete_future_and_duplicate_identity_fail_closed() -> None:
    assert capture_systemd_status_evidence([], captured_at_epoch_seconds=100).status == "PARTIAL"
    assert (
        capture_systemd_status_evidence(
            [_unit(1, complete=False)], captured_at_epoch_seconds=100
        ).status
        == "PARTIAL"
    )
    assert (
        capture_systemd_status_evidence(
            [_unit(1, observed_at_epoch_seconds=101)], captured_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )
    assert (
        capture_systemd_status_evidence([_unit(1), _unit(1)], captured_at_epoch_seconds=100).status
        == "TAMPERED"
    )
    assert (
        capture_systemd_status_evidence(
            [_unit(1), _unit(1, scope="SYSTEM")], captured_at_epoch_seconds=100
        ).status
        == "CAPTURED"
    )


def test_unit_capture_safety_tampering_and_systemctl_surfaces_fail_closed() -> None:
    with pytest.raises(SystemdStatusEvidenceCaptureError, match="UNIT_HASH_MISMATCH"):
        capture_systemd_status_evidence(
            [replace(_unit(1), active_state="FAILED")], captured_at_epoch_seconds=100
        )
    result = capture_systemd_status_evidence([_unit(1)], captured_at_epoch_seconds=100)
    with pytest.raises(SystemdStatusEvidenceCaptureError, match="CAPTURE_HASH_MISMATCH"):
        validate_systemd_status_capture(replace(result, reasons=("FORGED",)))
    with pytest.raises(SystemdStatusEvidenceCaptureError, match="SAFETY_BOUNDARY"):
        validate_systemd_status_capture(replace(result, service_control_authorized=True))
    forbidden = {
        "systemctl",
        "dbus",
        "open",
        "run",
        "popen",
        "subprocess",
        "start",
        "stop",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(capture_systemd_status_evidence.__code__.co_names)
