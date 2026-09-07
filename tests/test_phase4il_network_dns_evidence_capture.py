from dataclasses import replace

import pytest
from kalshi_predictor.workstation.network_dns_evidence_capture import (
    NetworkDnsEvidenceCaptureError,
    capture_network_dns_evidence,
    make_network_probe_evidence,
    validate_network_dns_capture,
)


def _probe(n, kind="DNS", **overrides):
    fields = dict(
        probe_id_hash=str(n) * 64,
        target_id_hash="a" * 64,
        probe_type=kind,
        observed_at_epoch_seconds=100 + n,
        succeeded=True,
        latency_milliseconds=10,
        result_code="SUCCESS",
        complete=True,
    )
    fields.update(overrides)
    return make_network_probe_evidence(**fields)


def test_all_probe_types_are_canonical_redacted_and_non_authorizing() -> None:
    records = [
        _probe(index + 1, kind) for index, kind in enumerate(("DNS", "ROUTE", "TCP", "TLS", "HTTP"))
    ]
    first = capture_network_dns_evidence(
        list(reversed(records)), window_start_epoch_seconds=100, window_end_epoch_seconds=200
    )
    second = capture_network_dns_evidence(
        records, window_start_epoch_seconds=100, window_end_epoch_seconds=200
    )
    assert first == second and first.status == "CAPTURED" and first.success_count == 5
    assert first.targets_redacted and not first.raw_responses_retained
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_network_dns_capture(first)


def test_exact_probe_and_time_bounds_pass_then_excess_refuses() -> None:
    records = [_probe(1, observed_at_epoch_seconds=100), _probe(2, observed_at_epoch_seconds=200)]
    assert (
        capture_network_dns_evidence(
            records, window_start_epoch_seconds=100, window_end_epoch_seconds=200, max_probes=2
        ).status
        == "CAPTURED"
    )
    with pytest.raises(NetworkDnsEvidenceCaptureError, match="BOUND_EXCEEDED"):
        capture_network_dns_evidence(
            records, window_start_epoch_seconds=100, window_end_epoch_seconds=200, max_probes=1
        )
    assert (
        capture_network_dns_evidence(
            [_probe(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=100
        ).status
        == "REFUSED"
    )


def test_empty_incomplete_duplicate_contradictory_and_tampered_fail_closed() -> None:
    assert (
        capture_network_dns_evidence(
            [], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        ).status
        == "PARTIAL"
    )
    assert (
        capture_network_dns_evidence(
            [_probe(1, complete=False)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        ).status
        == "PARTIAL"
    )
    assert (
        capture_network_dns_evidence(
            [_probe(1), _probe(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
        ).status
        == "TAMPERED"
    )
    assert (
        capture_network_dns_evidence(
            [_probe(1, result_code="DNS_FAILURE")],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
        ).status
        == "TAMPERED"
    )
    with pytest.raises(NetworkDnsEvidenceCaptureError, match="PROBE_HASH_MISMATCH"):
        capture_network_dns_evidence(
            [replace(_probe(1), succeeded=False)],
            window_start_epoch_seconds=0,
            window_end_epoch_seconds=200,
        )


def test_capture_safety_tampering_and_network_surfaces_fail_closed() -> None:
    result = capture_network_dns_evidence(
        [_probe(1)], window_start_epoch_seconds=0, window_end_epoch_seconds=200
    )
    with pytest.raises(NetworkDnsEvidenceCaptureError, match="CAPTURE_HASH_MISMATCH"):
        validate_network_dns_capture(replace(result, reasons=("FORGED",)))
    with pytest.raises(NetworkDnsEvidenceCaptureError, match="SAFETY_BOUNDARY"):
        validate_network_dns_capture(replace(result, raw_responses_retained=True))
    forbidden = {
        "socket",
        "connect",
        "getaddrinfo",
        "requests",
        "httpx",
        "open",
        "run",
        "subprocess",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(capture_network_dns_evidence.__code__.co_names)
