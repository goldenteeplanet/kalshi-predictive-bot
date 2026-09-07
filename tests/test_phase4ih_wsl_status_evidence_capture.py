from dataclasses import replace

import pytest
from kalshi_predictor.workstation.wsl_status_evidence_capture import (
    WslStatusEvidenceCaptureError,
    capture_wsl_status_evidence,
    make_wsl_distribution_evidence,
    validate_wsl_status_capture,
)


def _distro(n, **overrides):
    fields = dict(
        distribution_id_hash=str(n) * 64,
        state="RUNNING",
        wsl_version=2,
        is_default=n == 1,
        observed_at_epoch_seconds=100,
        complete=True,
    )
    fields.update(overrides)
    return make_wsl_distribution_evidence(**fields)


def test_capture_is_deterministic_redacted_and_non_authorizing() -> None:
    first = capture_wsl_status_evidence([_distro(2), _distro(1)], captured_at_epoch_seconds=100)
    second = capture_wsl_status_evidence([_distro(1), _distro(2)], captured_at_epoch_seconds=100)
    assert first == second and first.status == "CAPTURED" and first.running_count == 2
    assert first.distribution_names_redacted and not first.raw_output_retained
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_wsl_status_capture(first)


def test_exact_distribution_bound_passes_then_excess_raises() -> None:
    records = [_distro(1), _distro(2)]
    assert (
        capture_wsl_status_evidence(
            records, captured_at_epoch_seconds=100, max_distributions=2
        ).status
        == "CAPTURED"
    )
    with pytest.raises(WslStatusEvidenceCaptureError, match="BOUND_EXCEEDED"):
        capture_wsl_status_evidence(records, captured_at_epoch_seconds=100, max_distributions=1)


def test_empty_missing_default_incomplete_future_duplicate_and_multiple_defaults_fail_closed() -> (
    None
):
    assert capture_wsl_status_evidence([], captured_at_epoch_seconds=100).status == "PARTIAL"
    assert (
        capture_wsl_status_evidence([_distro(2)], captured_at_epoch_seconds=100).status == "PARTIAL"
    )
    assert (
        capture_wsl_status_evidence(
            [_distro(1, complete=False)], captured_at_epoch_seconds=100
        ).status
        == "PARTIAL"
    )
    assert (
        capture_wsl_status_evidence(
            [_distro(1, observed_at_epoch_seconds=101)], captured_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )
    assert (
        capture_wsl_status_evidence([_distro(1), _distro(1)], captured_at_epoch_seconds=100).status
        == "TAMPERED"
    )
    assert (
        capture_wsl_status_evidence(
            [_distro(1), _distro(2, is_default=True)], captured_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )


def test_evidence_capture_safety_tampering_and_wsl_surfaces_fail_closed() -> None:
    with pytest.raises(WslStatusEvidenceCaptureError, match="DISTRIBUTION_HASH_MISMATCH"):
        capture_wsl_status_evidence(
            [replace(_distro(1), state="STOPPED")], captured_at_epoch_seconds=100
        )
    result = capture_wsl_status_evidence([_distro(1)], captured_at_epoch_seconds=100)
    with pytest.raises(WslStatusEvidenceCaptureError, match="CAPTURE_HASH_MISMATCH"):
        validate_wsl_status_capture(replace(result, reasons=("FORGED",)))
    with pytest.raises(WslStatusEvidenceCaptureError, match="SAFETY_BOUNDARY"):
        validate_wsl_status_capture(replace(result, raw_output_retained=True))
    forbidden = {
        "wsl",
        "wsl.exe",
        "open",
        "run",
        "popen",
        "subprocess",
        "shutdown",
        "terminate",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(capture_wsl_status_evidence.__code__.co_names)
