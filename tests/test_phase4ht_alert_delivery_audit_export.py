from dataclasses import replace

import pytest

from kalshi_predictor.workstation.alert_delivery_audit_export import (
    AlertDeliveryAuditExportError,
    export_alert_delivery_audit,
    make_alert_delivery_audit_record,
    validate_alert_delivery_audit_bundle,
)


def _record(n: int, **overrides):
    fields = dict(
        event_id_hash=f"{n:x}" * 64,
        incident_id_hash="a" * 64,
        decision_hash="b" * 64,
        occurred_at_epoch_seconds=100 + n,
        channel_code="WINDOWS_TOAST",
        outcome="DELIVERED",
        complete=True,
    )
    fields.update(overrides)
    return make_alert_delivery_audit_record(**fields)


def test_export_is_sorted_deterministic_redacted_and_non_authorizing() -> None:
    first = export_alert_delivery_audit(
        [_record(2), _record(1)], window_start_epoch_seconds=100, window_end_epoch_seconds=200
    )
    second = export_alert_delivery_audit(
        [_record(1), _record(2)], window_start_epoch_seconds=100, window_end_epoch_seconds=200
    )
    assert first == second
    assert first.identifiers_redacted and first.read_only
    assert not any(
        (
            first.alert_delivery_authorized,
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_alert_delivery_audit_bundle(first)


def test_empty_export_and_exact_window_boundaries_are_valid() -> None:
    empty = export_alert_delivery_audit(
        [], window_start_epoch_seconds=100, window_end_epoch_seconds=100
    )
    exact = export_alert_delivery_audit(
        [_record(1, occurred_at_epoch_seconds=100)],
        window_start_epoch_seconds=100,
        window_end_epoch_seconds=100,
    )
    assert empty.record_count == 0
    assert exact.record_count == 1


def test_bounds_duplicates_incomplete_and_outside_window_fail_closed() -> None:
    with pytest.raises(AlertDeliveryAuditExportError, match="BOUND_EXCEEDED"):
        export_alert_delivery_audit(
            [_record(1), _record(2)],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
            max_records=1,
        )
    with pytest.raises(AlertDeliveryAuditExportError, match="DUPLICATE"):
        export_alert_delivery_audit(
            [_record(1), _record(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        )
    with pytest.raises(AlertDeliveryAuditExportError, match="INCOMPLETE"):
        export_alert_delivery_audit(
            [_record(1, complete=False)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        )
    with pytest.raises(AlertDeliveryAuditExportError, match="OUTSIDE_WINDOW"):
        export_alert_delivery_audit(
            [_record(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=100
        )


def test_malformed_and_tampered_records_fail_closed() -> None:
    with pytest.raises(AlertDeliveryAuditExportError, match="FIELD_INVALID"):
        _record(1, channel_code="toast channel")
    with pytest.raises(AlertDeliveryAuditExportError, match="HASH_MISMATCH"):
        export_alert_delivery_audit(
            [replace(_record(1), outcome="FAILED")],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
        )


def test_bundle_content_hash_and_safety_tampering_are_detected() -> None:
    bundle = export_alert_delivery_audit(
        [_record(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
    )
    with pytest.raises(AlertDeliveryAuditExportError, match="HASH_MISMATCH"):
        validate_alert_delivery_audit_bundle(replace(bundle, export_hash="0" * 64))
    with pytest.raises(AlertDeliveryAuditExportError, match="SAFETY_BOUNDARY"):
        validate_alert_delivery_audit_bundle(replace(bundle, host_restart_authorized=True))


def test_export_has_no_io_delivery_restart_or_execution_surface() -> None:
    forbidden = {
        "open",
        "subprocess",
        "socket",
        "requests",
        "send",
        "notify",
        "restart",
        "reboot",
        "shutdown",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(export_alert_delivery_audit.__code__.co_names)
