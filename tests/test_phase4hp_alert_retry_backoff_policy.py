from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.alert_retry_backoff_policy import (
    AlertRetryBackoffPolicyError,
    evaluate_alert_retry_backoff,
    make_alert_delivery_attempt,
    validate_alert_retry_decision,
)
from kalshi_predictor.workstation.alert_severity_deduplication import (
    evaluate_alert_severity_and_deduplication,
    make_alert_candidate,
)


def test_first_attempt_is_immediately_ready_but_delivery_is_not_authorized() -> None:
    alert = _alert()
    first = evaluate_alert_retry_backoff(alert, [], evaluated_at_epoch_seconds=100)
    second = evaluate_alert_retry_backoff(alert, [], evaluated_at_epoch_seconds=100)
    validate_alert_retry_decision(first)
    assert first.status == "READY"
    assert first.next_attempt_number == 1
    assert first.next_eligible_at_epoch_seconds == 100
    assert first.decision_hash == second.decision_hash
    assert first.alert_delivery_authorized is False


def test_backoff_waits_then_allows_at_exact_boundary() -> None:
    alert = _alert()
    attempt = _attempt(alert, number=1, at=100)
    wait = evaluate_alert_retry_backoff(alert, [attempt], evaluated_at_epoch_seconds=129)
    assert wait.status == "WAIT"
    assert wait.next_eligible_at_epoch_seconds == 130
    exact = evaluate_alert_retry_backoff(alert, [attempt], evaluated_at_epoch_seconds=130)
    assert exact.status == "READY"
    assert exact.next_attempt_number == 2


def test_exponential_delay_caps_and_attempt_budget_exhausts() -> None:
    alert = _alert()
    attempts = [
        _attempt(alert, number=1, at=100),
        _attempt(alert, number=2, at=300),
    ]
    capped = evaluate_alert_retry_backoff(
        alert,
        attempts,
        evaluated_at_epoch_seconds=599,
        base_backoff_seconds=200,
        max_backoff_seconds=300,
    )
    assert capped.status == "WAIT"
    assert capped.next_eligible_at_epoch_seconds == 600
    four = [
        _attempt(alert, number=1, at=100),
        _attempt(alert, number=2, at=130),
        _attempt(alert, number=3, at=190),
        _attempt(alert, number=4, at=310),
    ]
    exhausted = evaluate_alert_retry_backoff(alert, four, evaluated_at_epoch_seconds=400)
    assert exhausted.status == "EXHAUSTED"
    assert exhausted.retry_candidate_ready is False


def test_delivered_incomplete_and_non_emittable_histories_are_terminal() -> None:
    alert = _alert()
    delivered = _attempt(alert, number=1, at=100, outcome="DELIVERED")
    assert (
        evaluate_alert_retry_backoff(alert, [delivered], evaluated_at_epoch_seconds=101).status
        == "DELIVERED"
    )
    incomplete = _attempt(alert, number=1, at=100, complete=False)
    assert (
        evaluate_alert_retry_backoff(alert, [incomplete], evaluated_at_epoch_seconds=101).status
        == "INCOMPLETE"
    )
    denied_alert = _alert(category="UNKNOWN")
    assert (
        evaluate_alert_retry_backoff(denied_alert, [], evaluated_at_epoch_seconds=100).status
        == "DENIED"
    )


def test_sequence_time_binding_future_and_post_delivery_fail_closed() -> None:
    alert = _alert()
    with pytest.raises(AlertRetryBackoffPolicyError, match="ATTEMPT_SEQUENCE_INVALID"):
        evaluate_alert_retry_backoff(
            alert, [_attempt(alert, number=2, at=100)], evaluated_at_epoch_seconds=200
        )
    other = _alert(reason="OTHER")
    with pytest.raises(AlertRetryBackoffPolicyError, match="ATTEMPT_ALERT_BINDING_MISMATCH"):
        evaluate_alert_retry_backoff(
            alert, [_attempt(other, number=1, at=100)], evaluated_at_epoch_seconds=200
        )
    with pytest.raises(AlertRetryBackoffPolicyError, match="ATTEMPT_FROM_FUTURE"):
        evaluate_alert_retry_backoff(
            alert, [_attempt(alert, number=1, at=201)], evaluated_at_epoch_seconds=200
        )
    history = [
        _attempt(alert, number=1, at=100, outcome="DELIVERED"),
        _attempt(alert, number=2, at=101),
    ]
    with pytest.raises(AlertRetryBackoffPolicyError, match="ATTEMPT_AFTER_DELIVERY"):
        evaluate_alert_retry_backoff(alert, history, evaluated_at_epoch_seconds=200)


def test_bounds_attempt_alert_decision_and_safety_tampering_fail_closed() -> None:
    alert = _alert()
    with pytest.raises(AlertRetryBackoffPolicyError, match="POLICY_BOUND_INVALID"):
        evaluate_alert_retry_backoff(
            alert, [], evaluated_at_epoch_seconds=100, base_backoff_seconds=True
        )
    attempt = _attempt(alert, number=1, at=100)
    with pytest.raises(AlertRetryBackoffPolicyError, match="ATTEMPT_HASH_MISMATCH"):
        evaluate_alert_retry_backoff(
            alert, [replace(attempt, outcome="DELIVERED")], evaluated_at_epoch_seconds=200
        )
    with pytest.raises(AlertRetryBackoffPolicyError, match="ALERT_RESULT_INVALID"):
        evaluate_alert_retry_backoff(
            replace(alert, result_hash="0" * 64), [], evaluated_at_epoch_seconds=100
        )
    decision = evaluate_alert_retry_backoff(alert, [], evaluated_at_epoch_seconds=100)
    with pytest.raises(AlertRetryBackoffPolicyError, match="DECISION_HASH_MISMATCH"):
        validate_alert_retry_decision(replace(decision, decision_hash="0" * 64))
    with pytest.raises(AlertRetryBackoffPolicyError, match="DECISION_SAFETY_BOUNDARY_INVALID"):
        validate_alert_retry_decision(replace(decision, notification_sent=True))


def test_policy_has_no_delivery_file_service_or_restart_surface() -> None:
    names = set(evaluate_alert_retry_backoff.__code__.co_names)
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


def _alert(*, category="WSL_LIVENESS", reason="WSL_UNAVAILABLE"):
    candidate = make_alert_candidate(
        incident_id="incident-1",
        category=category,
        reason_code=reason,
        observed_at_epoch_seconds=100,
        evidence_age_seconds=1,
        evidence_hash="a" * 64,
        complete=True,
        source_identity_hash="b" * 64,
    )
    return evaluate_alert_severity_and_deduplication(candidate, [], evaluated_at_epoch_seconds=100)


def _attempt(alert, *, number, at, outcome="FAILED", complete=True):
    return make_alert_delivery_attempt(
        attempt_number=number,
        attempted_at_epoch_seconds=at,
        outcome=outcome,
        channel="windows-toast",
        alert_result_hash=alert.result_hash,
        complete=complete,
    )
