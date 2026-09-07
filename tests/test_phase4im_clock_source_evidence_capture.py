from dataclasses import replace

import pytest
from kalshi_predictor.workstation.clock_source_evidence_capture import (
    ClockSourceEvidenceCaptureError,
    capture_clock_source_evidence,
    make_clock_source_evidence,
    validate_clock_source_capture,
)


def _source(n, source_type="NTP", **overrides):
    fields = dict(
        source_id_hash=str(n) * 64,
        source_type=source_type,
        observed_at_epoch_seconds=100,
        offset_milliseconds=n,
        uncertainty_milliseconds=1,
        stratum=2 if source_type == "NTP" else None,
        synchronized=True,
        complete=True,
    )
    fields.update(overrides)
    return make_clock_source_evidence(**fields)


def test_all_source_types_are_deterministic_redacted_and_non_authorizing() -> None:
    records = [_source(1), _source(2, "WINDOWS_HOST"), _source(3, "SIGNED_TIME")]
    first = capture_clock_source_evidence(list(reversed(records)), captured_at_epoch_seconds=100)
    second = capture_clock_source_evidence(records, captured_at_epoch_seconds=100)
    assert first == second and first.status == "CAPTURED" and first.synchronized_source_count == 3
    assert first.source_addresses_redacted and not first.raw_output_retained
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_clock_source_capture(first)


def test_exact_source_and_offset_bounds_pass_then_excess_refuses() -> None:
    records = [_source(1, offset_milliseconds=100), _source(2, offset_milliseconds=-100)]
    assert (
        capture_clock_source_evidence(
            records,
            captured_at_epoch_seconds=100,
            max_sources=2,
            maximum_absolute_offset_milliseconds=100,
        ).status
        == "CAPTURED"
    )
    with pytest.raises(ClockSourceEvidenceCaptureError, match="COUNT_BOUND_EXCEEDED"):
        capture_clock_source_evidence(records, captured_at_epoch_seconds=100, max_sources=1)
    assert (
        capture_clock_source_evidence(
            [_source(1, offset_milliseconds=101)],
            captured_at_epoch_seconds=100,
            maximum_absolute_offset_milliseconds=100,
        ).status
        == "REFUSED"
    )


def test_empty_incomplete_future_duplicate_and_invalid_stratum_fail_closed() -> None:
    assert capture_clock_source_evidence([], captured_at_epoch_seconds=100).status == "PARTIAL"
    assert (
        capture_clock_source_evidence(
            [_source(1, complete=False)], captured_at_epoch_seconds=100
        ).status
        == "PARTIAL"
    )
    assert (
        capture_clock_source_evidence(
            [_source(1, observed_at_epoch_seconds=101)], captured_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )
    assert (
        capture_clock_source_evidence(
            [_source(1), _source(1)], captured_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )
    with pytest.raises(ClockSourceEvidenceCaptureError, match="FIELD_INVALID"):
        _source(1, "WINDOWS_HOST", stratum=2)


def test_source_capture_safety_tampering_and_clock_surfaces_fail_closed() -> None:
    with pytest.raises(ClockSourceEvidenceCaptureError, match="SOURCE_HASH_MISMATCH"):
        capture_clock_source_evidence(
            [replace(_source(1), synchronized=False)], captured_at_epoch_seconds=100
        )
    result = capture_clock_source_evidence([_source(1)], captured_at_epoch_seconds=100)
    with pytest.raises(ClockSourceEvidenceCaptureError, match="CAPTURE_HASH_MISMATCH"):
        validate_clock_source_capture(replace(result, reasons=("FORGED",)))
    with pytest.raises(ClockSourceEvidenceCaptureError, match="SAFETY_BOUNDARY"):
        validate_clock_source_capture(replace(result, raw_output_retained=True))
    forbidden = {
        "ntp",
        "w32tm",
        "timedatectl",
        "clock_settime",
        "socket",
        "open",
        "run",
        "subprocess",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(capture_clock_source_evidence.__code__.co_names)
