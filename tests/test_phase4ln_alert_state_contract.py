from __future__ import annotations

import hashlib
import json

import pytest

from scripts.local.phase4ln_alert_state_contract import evaluate_alert


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _ledger(severity: str = "WARNING") -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "phase4lm.runtime-observation-ledger.v1",
        "verdict": "PASS",
        "errors": [],
        "records": [
            {
                "event_kind": "service_stopped",
                "severity": severity,
                "snapshot_sha256": "a" * 64,
                "classification_sha256": "b" * 64,
            }
        ],
        "summary": {},
        "safety": {},
    }
    value["ledger_sha256"] = _digest(value)
    return value


def _evaluate(ledger=None, history=None, ack=None, at="2026-08-28T20:00:00Z", cooldown=900):
    return evaluate_alert(
        ledger or _ledger(), history or [], ack, evaluated_at=at, cooldown_seconds=cooldown
    )


def test_new_warning_emits_deterministically() -> None:
    first = _evaluate()
    assert first == _evaluate()
    assert (first["decision"], first["reason"]) == ("EMIT", "NEW_ALERT")


@pytest.mark.parametrize("severity", ["BENIGN", "EXPECTED_TRANSIENT"])
def test_non_alerting_severity_is_suppressed(severity: str) -> None:
    assert _evaluate(_ledger(severity))["reason"] == "NON_ALERTING_SEVERITY"


def test_duplicate_is_suppressed_during_cooldown_then_emitted() -> None:
    first = _evaluate()
    history = [
        {
            "alert_key": first["alert_key"],
            "severity": "WARNING",
            "emitted_at": "2026-08-28T20:00:00Z",
        }
    ]
    during = _evaluate(history=history, at="2026-08-28T20:10:00Z")
    after = _evaluate(history=history, at="2026-08-28T20:16:00Z")
    assert during["reason"] == "DUPLICATE_IN_COOLDOWN"
    assert after["reason"] == "COOLDOWN_EXPIRED"


def test_escalation_bypasses_cooldown() -> None:
    current = _evaluate(_ledger("CRITICAL"))
    history = [
        {
            "alert_key": current["alert_key"],
            "severity": "WARNING",
            "emitted_at": "2026-08-28T19:59:59Z",
        }
    ]
    assert _evaluate(_ledger("CRITICAL"), history)["reason"] == "SEVERITY_ESCALATED"


def test_latest_matching_history_wins_independent_of_input_order() -> None:
    first = _evaluate()
    recent = {
        "alert_key": first["alert_key"],
        "severity": "WARNING",
        "emitted_at": "2026-08-28T19:59:00Z",
    }
    old = dict(recent, emitted_at="2026-08-28T18:00:00Z")
    assert _evaluate(history=[recent, old])["reason"] == "DUPLICATE_IN_COOLDOWN"


def test_active_ack_suppresses_and_expired_ack_does_not() -> None:
    result = _evaluate()
    active = {
        "alert_key": result["alert_key"],
        "acknowledged_at": "2026-08-28T19:59:00Z",
        "expires_at": "2026-08-28T21:00:00Z",
    }
    expired = dict(active, expires_at="2026-08-28T19:59:30Z")
    assert _evaluate(ack=active)["reason"] == "ACTIVE_ACKNOWLEDGEMENT"
    assert _evaluate(ack=expired)["reason"] == "NEW_ALERT"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda ledger: ledger.update(schema="wrong"),
        lambda ledger: ledger.update(verdict="REFUSE"),
        lambda ledger: ledger["records"][0].update(severity="CRITICAL"),
    ],
)
def test_invalid_or_replayed_ledger_fails_closed(mutation) -> None:
    ledger = _ledger()
    mutation(ledger)
    assert _evaluate(ledger)["decision"] == "REFUSE"


def test_future_history_and_overlong_ack_refuse() -> None:
    first = _evaluate()
    history = [
        {
            "alert_key": first["alert_key"],
            "severity": "WARNING",
            "emitted_at": "2027-01-01T00:00:00Z",
        }
    ]
    ack = {
        "alert_key": first["alert_key"],
        "acknowledged_at": "2026-08-28T19:00:00Z",
        "expires_at": "2026-08-30T19:00:01Z",
    }
    assert _evaluate(history=history)["decision"] == "REFUSE"
    assert _evaluate(ack=ack)["decision"] == "REFUSE"


def test_contract_has_no_delivery_or_action_capability() -> None:
    safety = _evaluate()["safety"]
    assert safety["recommendation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "recommendation_only")
