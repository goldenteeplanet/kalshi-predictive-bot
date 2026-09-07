from __future__ import annotations

import copy

from scripts.local.phase4pa_alert_routing import build_alert
from scripts.local.phase4pb_alert_delivery_state import (
    apply_event,
    certify_delivery_state_machine,
    event,
    new_state,
    replay,
)


def _alert() -> dict[str, object]:
    return build_alert(
        refusal_code="BOT_SERVICE_MISSING",
        stage="BOT_ACTIVE",
        occurrence=3,
        recovery_succeeded=False,
    )


def test_lost_ack_retries_then_acknowledges_exact_attempt() -> None:
    alert = _alert()
    attempt_one = f"{alert['alert_sha256']}:1"
    events = [
        event("a1", "ATTEMPT", 0),
        event("t1", "TIMEOUT", 1, attempt_id=attempt_one),
        event("a2", "ATTEMPT", 2),
        event("ack2", "ACK", 3, attempt_id=f"{alert['alert_sha256']}:2"),
    ]
    state = replay(alert, events)
    assert state["status"] == "ACKED"
    assert state["attempt"] == 2
    assert state["real_notifications_sent"] == 0


def test_identical_duplicate_is_idempotent_but_conflicting_duplicate_refuses() -> None:
    alert = _alert()
    attempt = event("same", "ATTEMPT", 0)
    once = apply_event(new_state(alert), attempt)
    assert apply_event(once, attempt) == once
    conflicting = event("same", "ACK", 1, attempt_id=once["active_attempt_id"])
    refused = apply_event(once, conflicting)
    assert refused["status"] == "REFUSED"
    assert "CONFLICTING_DUPLICATE_EVENT" in refused["errors"]


def test_delayed_stale_ack_and_reordered_events_fail_closed() -> None:
    alert = _alert()
    first = replay(
        alert,
        [
            event("a1", "ATTEMPT", 0),
            event("t1", "TIMEOUT", 1, attempt_id=f"{alert['alert_sha256']}:1"),
            event("a2", "ATTEMPT", 2),
        ],
    )
    stale = apply_event(
        first,
        event("late", "ACK", 3, attempt_id=f"{alert['alert_sha256']}:1"),
    )
    assert stale["status"] == "REFUSED"
    reordered = replay(alert, [event("ack-first", "ACK", 0, attempt_id="nope")])
    assert reordered["status"] == "REFUSED"


def test_retry_exhaustion_and_concurrent_escalation_are_safe() -> None:
    alert = _alert()
    events = []
    tick = 0
    for number in (1, 2, 3):
        events.extend(
            [
                event(f"a{number}", "ATTEMPT", tick),
                event(
                    f"t{number}",
                    "TIMEOUT",
                    tick + 1,
                    attempt_id=f"{alert['alert_sha256']}:{number}",
                ),
            ]
        )
        tick += 3
    exhausted = replay(alert, events)
    assert exhausted["status"] == "ESCALATED"
    concurrent = apply_event(exhausted, event("operator", "ESCALATE", tick))
    assert concurrent["status"] == "REFUSED"


def test_restart_replay_is_deterministic_and_corruption_refuses() -> None:
    alert = _alert()
    events = [event("a1", "ATTEMPT", 0)]
    assert replay(alert, events) == replay(alert, events)
    corrupt = copy.deepcopy(events[0])
    corrupt["tick"] = 99
    assert replay(alert, [corrupt])["status"] == "REFUSED"
    corrupted_state = replay(alert, events)
    corrupted_state["attempt"] = 99
    refused = apply_event(corrupted_state, event("next", "TIMEOUT", 1, attempt_id="wrong"))
    assert refused["status"] == "REFUSED"
    assert "STATE_HASH_MISMATCH" in refused["errors"]


def test_delivery_certificate_passes_offline() -> None:
    result = certify_delivery_state_machine()
    assert result["verdict"] == "PASS"
    assert result["lost_ack_retried"] is True
    assert result["restart_replay_identical"] is True
    assert result["retry_exhaustion_escalated"] is True
    assert result["real_notifications_sent"] == 0
    assert all(value is False for key, value in result["safety"].items() if key != "offline_only")
