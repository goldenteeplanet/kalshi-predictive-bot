from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.alert_rate_limit_storm_control import (
    AlertRateLimitStormControlError,
    evaluate_alert_rate_limit_and_storm_control,
    make_alert_emission_event,
    validate_alert_admission_decision,
)
from kalshi_predictor.workstation.alert_retry_backoff_policy import evaluate_alert_retry_backoff
from kalshi_predictor.workstation.alert_severity_deduplication import (
    evaluate_alert_severity_and_deduplication,
    make_alert_candidate,
)


def test_empty_history_allows_candidate_without_delivery_authority() -> None:
    retry = _retry()
    first = evaluate_alert_rate_limit_and_storm_control(
        retry, "WARNING", [], evaluated_at_epoch_seconds=100
    )
    second = evaluate_alert_rate_limit_and_storm_control(
        retry, "WARNING", [], evaluated_at_epoch_seconds=100
    )
    validate_alert_admission_decision(first)
    assert first.status == "ALLOW"
    assert first.decision_hash == second.decision_hash
    assert first.admission_candidate_ready is True
    assert first.alert_delivery_authorized is False


def test_noncritical_capacity_is_reserved_for_critical_alerts() -> None:
    retry = _retry()
    history = [_event(index, 90 + index, "WARNING") for index in range(1, 4)]
    warning = evaluate_alert_rate_limit_and_storm_control(
        retry, "WARNING", history, evaluated_at_epoch_seconds=100
    )
    assert warning.status == "RATE_LIMITED"
    assert warning.reasons == ("ALERT_NONCRITICAL_CAPACITY_RESERVED",)
    critical = evaluate_alert_rate_limit_and_storm_control(
        retry, "CRITICAL", history, evaluated_at_epoch_seconds=100
    )
    assert critical.status == "ALLOW"


def test_short_limit_and_exact_window_expiry() -> None:
    retry = _retry()
    history = [_event(index, 95 + index, "CRITICAL") for index in range(1, 6)]
    assert (
        evaluate_alert_rate_limit_and_storm_control(
            retry, "CRITICAL", history, evaluated_at_epoch_seconds=101
        ).status
        == "RATE_LIMITED"
    )
    exact = [_event(1, 40, "WARNING")]
    result = evaluate_alert_rate_limit_and_storm_control(
        retry, "WARNING", exact, evaluated_at_epoch_seconds=100
    )
    assert result.status == "ALLOW"
    assert result.short_window_count == 0


def test_storm_limit_is_hard_and_exact_storm_expiry_is_excluded() -> None:
    retry = _retry()
    storm = [_event(index, index, "CRITICAL") for index in range(1, 21)]
    result = evaluate_alert_rate_limit_and_storm_control(
        retry, "CRITICAL", storm, evaluated_at_epoch_seconds=300
    )
    assert result.status == "STORM_SUPPRESSED"
    assert result.storm_window_count == 20
    exact = [_event(1, 1, "CRITICAL")]
    result = evaluate_alert_rate_limit_and_storm_control(
        retry, "CRITICAL", exact, evaluated_at_epoch_seconds=301
    )
    assert result.storm_window_count == 0


def test_nonready_incomplete_order_bounds_duplicates_and_future_fail_closed() -> None:
    retry = _retry()
    incomplete = [_event(1, 99, "WARNING", complete=False)]
    assert (
        evaluate_alert_rate_limit_and_storm_control(
            retry, "WARNING", incomplete, evaluated_at_epoch_seconds=100
        ).status
        == "INCOMPLETE"
    )
    nonready = replace(
        retry,
        status="WAIT",
        reasons=("ALERT_BACKOFF_ACTIVE",),
        retry_candidate_ready=False,
    )
    nonready = _rehash_retry(nonready)
    assert (
        evaluate_alert_rate_limit_and_storm_control(
            nonready, "WARNING", [], evaluated_at_epoch_seconds=100
        ).status
        == "DENIED"
    )
    event = _event(1, 99, "WARNING")
    with pytest.raises(AlertRateLimitStormControlError, match="HISTORY_BOUND_EXCEEDED"):
        evaluate_alert_rate_limit_and_storm_control(
            retry,
            "WARNING",
            [event, _event(2, 98, "WARNING")],
            evaluated_at_epoch_seconds=100,
            max_history_records=1,
        )
    with pytest.raises(AlertRateLimitStormControlError, match="HISTORY_EVENT_ID_DUPLICATE"):
        evaluate_alert_rate_limit_and_storm_control(
            retry, "WARNING", [event, event], evaluated_at_epoch_seconds=100
        )
    with pytest.raises(AlertRateLimitStormControlError, match="HISTORY_EVENT_FROM_FUTURE"):
        evaluate_alert_rate_limit_and_storm_control(
            retry, "WARNING", [_event(2, 101, "WARNING")], evaluated_at_epoch_seconds=100
        )


def test_event_retry_decision_and_safety_tampering_fail_closed() -> None:
    retry = _retry()
    event = _event(1, 99, "WARNING")
    with pytest.raises(AlertRateLimitStormControlError, match="HISTORY_EVENT_HASH_MISMATCH"):
        evaluate_alert_rate_limit_and_storm_control(
            retry,
            "WARNING",
            [replace(event, severity="CRITICAL")],
            evaluated_at_epoch_seconds=100,
        )
    with pytest.raises(AlertRateLimitStormControlError, match="RETRY_DECISION_INVALID"):
        evaluate_alert_rate_limit_and_storm_control(
            replace(retry, decision_hash="0" * 64),
            "WARNING",
            [],
            evaluated_at_epoch_seconds=100,
        )
    decision = evaluate_alert_rate_limit_and_storm_control(
        retry, "WARNING", [], evaluated_at_epoch_seconds=100
    )
    with pytest.raises(AlertRateLimitStormControlError, match="DECISION_HASH_MISMATCH"):
        validate_alert_admission_decision(replace(decision, decision_hash="0" * 64))
    with pytest.raises(AlertRateLimitStormControlError, match="DECISION_SAFETY_BOUNDARY_INVALID"):
        validate_alert_admission_decision(replace(decision, notification_sent=True))


def test_policy_has_no_delivery_file_service_or_restart_surface() -> None:
    names = set(evaluate_alert_rate_limit_and_storm_control.__code__.co_names)
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


def _retry():
    candidate = make_alert_candidate(
        incident_id="incident-1",
        category="WSL_LIVENESS",
        reason_code="WSL_UNAVAILABLE",
        observed_at_epoch_seconds=100,
        evidence_age_seconds=1,
        evidence_hash="a" * 64,
        complete=True,
        source_identity_hash="b" * 64,
    )
    alert = evaluate_alert_severity_and_deduplication(candidate, [], evaluated_at_epoch_seconds=100)
    return evaluate_alert_retry_backoff(alert, [], evaluated_at_epoch_seconds=100)


def _event(index, at, severity, *, complete=True):
    return make_alert_emission_event(
        event_id=f"event-{index}",
        emitted_at_epoch_seconds=at,
        severity=severity,
        alert_result_hash="c" * 64,
        complete=complete,
    )


def _rehash_retry(decision):
    import hashlib
    import json
    from dataclasses import asdict

    unsigned = asdict(decision)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = "phase4hp-alert-retry-backoff-policy-v1"
    unsigned["reasons"] = list(unsigned["reasons"])
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    return replace(decision, decision_hash=hashlib.sha256(encoded).hexdigest())
