from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.alert_severity_deduplication import (
    AlertSeverityDeduplicationError,
    evaluate_alert_severity_and_deduplication,
    make_alert_candidate,
    make_prior_alert_record,
    validate_alert_disposition_result,
)


def test_known_categories_receive_deterministic_severity_without_delivery() -> None:
    expected = {
        "PROTECTED_INVARIANT": "CRITICAL",
        "WRITER_EXCLUSIVITY": "CRITICAL",
        "WSL_LIVENESS": "WARNING",
        "SCHEDULER": "WARNING",
        "RECOVERY_SUCCEEDED": "INFO",
    }
    for category, severity in expected.items():
        candidate = _candidate(category=category)
        first = evaluate_alert_severity_and_deduplication(
            candidate, [], evaluated_at_epoch_seconds=200
        )
        second = evaluate_alert_severity_and_deduplication(
            candidate, [], evaluated_at_epoch_seconds=200
        )
        validate_alert_disposition_result(first)
        assert first.disposition == "EMIT"
        assert first.severity == severity
        assert first.result_hash == second.result_hash
        assert first.alert_delivery_authorized is False


def test_duplicate_inside_window_suppresses_and_exact_boundary_emits() -> None:
    candidate = _candidate()
    initial = evaluate_alert_severity_and_deduplication(
        candidate, [], evaluated_at_epoch_seconds=100
    )
    prior = _record(initial.deduplication_key_hash, emitted=100, severity="WARNING")
    suppressed = evaluate_alert_severity_and_deduplication(
        candidate, [prior], evaluated_at_epoch_seconds=399
    )
    assert suppressed.disposition == "SUPPRESS_DUPLICATE"
    exact = evaluate_alert_severity_and_deduplication(
        candidate, [prior], evaluated_at_epoch_seconds=400
    )
    assert exact.disposition == "EMIT"


def test_severity_escalation_and_resolution_bypass_deduplication() -> None:
    candidate = _candidate(category="PROTECTED_INVARIANT")
    initial = evaluate_alert_severity_and_deduplication(
        candidate, [], evaluated_at_epoch_seconds=100
    )
    lower = _record(initial.deduplication_key_hash, emitted=100, severity="WARNING")
    assert (
        evaluate_alert_severity_and_deduplication(
            candidate, [lower], evaluated_at_epoch_seconds=101
        ).disposition
        == "EMIT"
    )
    resolved = _record(
        initial.deduplication_key_hash, emitted=100, severity="CRITICAL", resolved=True
    )
    assert (
        evaluate_alert_severity_and_deduplication(
            candidate, [resolved], evaluated_at_epoch_seconds=101
        ).disposition
        == "EMIT"
    )


def test_stale_incomplete_unknown_and_future_candidates_fail_closed() -> None:
    assert (
        evaluate_alert_severity_and_deduplication(
            _candidate(age=121), [], evaluated_at_epoch_seconds=200
        ).disposition
        == "STALE"
    )
    assert (
        evaluate_alert_severity_and_deduplication(
            _candidate(complete=False), [], evaluated_at_epoch_seconds=200
        ).disposition
        == "INCOMPLETE"
    )
    assert (
        evaluate_alert_severity_and_deduplication(
            _candidate(category="UNKNOWN"), [], evaluated_at_epoch_seconds=200
        ).disposition
        == "DENY"
    )
    assert (
        evaluate_alert_severity_and_deduplication(
            _candidate(observed=201), [], evaluated_at_epoch_seconds=200
        ).disposition
        == "DENY"
    )


def test_history_order_bounds_duplicates_and_future_records_fail_closed() -> None:
    candidate = _candidate()
    key = evaluate_alert_severity_and_deduplication(
        candidate, [], evaluated_at_epoch_seconds=200
    ).deduplication_key_hash
    older = _record(key, record_id="a", emitted=50)
    newer = _record(key, record_id="b", emitted=100)
    first = evaluate_alert_severity_and_deduplication(
        candidate, [newer, older], evaluated_at_epoch_seconds=200
    )
    second = evaluate_alert_severity_and_deduplication(
        candidate, [older, newer], evaluated_at_epoch_seconds=200
    )
    assert first.result_hash == second.result_hash
    with pytest.raises(AlertSeverityDeduplicationError, match="HISTORY_BOUND_EXCEEDED"):
        evaluate_alert_severity_and_deduplication(
            candidate, [older, newer], evaluated_at_epoch_seconds=200, max_history_records=1
        )
    with pytest.raises(AlertSeverityDeduplicationError, match="HISTORY_RECORD_ID_DUPLICATE"):
        evaluate_alert_severity_and_deduplication(
            candidate, [older, older], evaluated_at_epoch_seconds=200
        )
    future = _record(key, emitted=201)
    with pytest.raises(AlertSeverityDeduplicationError, match="HISTORY_RECORD_FROM_FUTURE"):
        evaluate_alert_severity_and_deduplication(
            candidate, [future], evaluated_at_epoch_seconds=200
        )


def test_candidate_history_result_and_safety_tampering_fail_closed() -> None:
    candidate = _candidate()
    with pytest.raises(AlertSeverityDeduplicationError, match="CANDIDATE_HASH_MISMATCH"):
        evaluate_alert_severity_and_deduplication(
            replace(candidate, reason_code="OTHER"), [], evaluated_at_epoch_seconds=200
        )
    initial = evaluate_alert_severity_and_deduplication(
        candidate, [], evaluated_at_epoch_seconds=200
    )
    prior = _record(initial.deduplication_key_hash)
    with pytest.raises(AlertSeverityDeduplicationError, match="HISTORY_RECORD_HASH_MISMATCH"):
        evaluate_alert_severity_and_deduplication(
            candidate, [replace(prior, resolved=True)], evaluated_at_epoch_seconds=200
        )
    with pytest.raises(AlertSeverityDeduplicationError, match="RESULT_HASH_MISMATCH"):
        validate_alert_disposition_result(replace(initial, result_hash="0" * 64))
    with pytest.raises(AlertSeverityDeduplicationError, match="RESULT_SAFETY_BOUNDARY_INVALID"):
        validate_alert_disposition_result(replace(initial, notification_sent=True))


def test_policy_has_no_delivery_file_service_or_restart_surface() -> None:
    names = set(evaluate_alert_severity_and_deduplication.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "commit",
            "connect",
            "execute",
            "open",
            "restart",
            "shutdown",
            "show",
            "start",
            "stop",
            "systemctl",
            "toast",
            "write",
        }
    )


def _candidate(
    *,
    category="WSL_LIVENESS",
    reason="WSL_UNAVAILABLE",
    observed=100,
    age=1,
    complete=True,
):
    return make_alert_candidate(
        incident_id="incident-1",
        category=category,
        reason_code=reason,
        observed_at_epoch_seconds=observed,
        evidence_age_seconds=age,
        evidence_hash="a" * 64,
        complete=complete,
        source_identity_hash="b" * 64,
    )


def _record(key, *, record_id="record-1", emitted=100, severity="WARNING", resolved=False):
    return make_prior_alert_record(
        record_id=record_id,
        emitted_at_epoch_seconds=emitted,
        deduplication_key_hash=key,
        severity=severity,
        resolved=resolved,
    )
