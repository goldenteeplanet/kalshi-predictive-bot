from dataclasses import replace

import pytest

from kalshi_predictor.workstation.bounded_diagnostics_collector import (
    BoundedDiagnosticsCollectorError,
    collect_bounded_diagnostics,
    make_diagnostic_sample,
    validate_diagnostic_collection,
)


def _sample(n, **overrides):
    fields = dict(
        sample_id_hash=str(n) * 64,
        source_code="WSL_STATUS",
        captured_at_epoch_seconds=100 + n,
        content_hash="a" * 64,
        byte_count=100,
        line_count=5,
        truncated=False,
        complete=True,
    )
    fields.update(overrides)
    return make_diagnostic_sample(**fields)


def test_hash_only_collection_is_deterministic_redacted_and_non_authorizing() -> None:
    first = collect_bounded_diagnostics(
        [_sample(2), _sample(1)], window_start_epoch_seconds=100, window_end_epoch_seconds=200
    )
    second = collect_bounded_diagnostics(
        [_sample(1), _sample(2)], window_start_epoch_seconds=100, window_end_epoch_seconds=200
    )
    assert first == second and first.status == "COLLECTED" and first.diagnostics_complete
    assert first.raw_content_retained is False and first.identifiers_redacted
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_diagnostic_collection(first)


def test_exact_record_byte_line_and_time_bounds_pass() -> None:
    samples = [
        _sample(1, captured_at_epoch_seconds=100, byte_count=50, line_count=5),
        _sample(2, captured_at_epoch_seconds=200, byte_count=50, line_count=5),
    ]
    result = collect_bounded_diagnostics(
        samples,
        window_start_epoch_seconds=100,
        window_end_epoch_seconds=200,
        max_records=2,
        max_total_bytes=100,
        max_total_lines=10,
    )
    assert result.status == "COLLECTED"


def test_each_excess_bound_and_outside_window_is_refused() -> None:
    with pytest.raises(BoundedDiagnosticsCollectorError, match="RECORD_BOUND_EXCEEDED"):
        collect_bounded_diagnostics(
            [_sample(1), _sample(2)],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
            max_records=1,
        )
    assert (
        collect_bounded_diagnostics(
            [_sample(1, byte_count=101)],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
            max_total_bytes=100,
        ).status
        == "REFUSED"
    )
    assert (
        collect_bounded_diagnostics(
            [_sample(1, line_count=11)],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
            max_total_lines=10,
        ).status
        == "REFUSED"
    )
    assert (
        collect_bounded_diagnostics(
            [_sample(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=100
        ).status
        == "REFUSED"
    )


def test_partial_truncated_duplicate_and_tampered_samples_fail_closed() -> None:
    assert (
        collect_bounded_diagnostics(
            [_sample(1, complete=False)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        ).status
        == "PARTIAL"
    )
    assert (
        collect_bounded_diagnostics(
            [_sample(1, truncated=True)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        ).status
        == "PARTIAL"
    )
    assert (
        collect_bounded_diagnostics(
            [_sample(1), _sample(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        ).status
        == "TAMPERED"
    )
    with pytest.raises(BoundedDiagnosticsCollectorError, match="SAMPLE_HASH_MISMATCH"):
        collect_bounded_diagnostics(
            [replace(_sample(1), byte_count=999)],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
        )


def test_collection_safety_tampering_and_command_surfaces_fail_closed() -> None:
    result = collect_bounded_diagnostics(
        [_sample(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
    )
    with pytest.raises(BoundedDiagnosticsCollectorError, match="COLLECTION_HASH_MISMATCH"):
        validate_diagnostic_collection(replace(result, reasons=("FORGED",)))
    with pytest.raises(BoundedDiagnosticsCollectorError, match="SAFETY_BOUNDARY"):
        validate_diagnostic_collection(replace(result, raw_content_retained=True))
    forbidden = {
        "open",
        "run",
        "popen",
        "subprocess",
        "socket",
        "system",
        "restart",
        "reboot",
        "shutdown",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(collect_bounded_diagnostics.__code__.co_names)
