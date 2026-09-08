from __future__ import annotations

import copy

from scripts.local.phase4pa_alert_routing import build_alert
from scripts.local.phase4pb_alert_delivery_state import event
from scripts.local.phase4pc_alert_outbox import (
    CRASH_POINTS,
    certify_crash_windows,
    checkpoint,
    new_store,
    recover,
)


def _alert() -> dict[str, object]:
    return build_alert(
        refusal_code="BOT_SERVICE_MISSING",
        stage="BOT_ACTIVE",
        occurrence=3,
        recovery_succeeded=False,
    )


def test_every_checkpoint_crash_window_recovers_without_losing_alert() -> None:
    result = certify_crash_windows(_alert())
    assert result["verdict"] == "PASS"
    assert set(result["crash_windows"]) == set(CRASH_POINTS)
    assert result["lost_critical_alerts"] == 0
    assert result["exactly_once_logical_handling"] is True


def test_recovery_is_exactly_once_for_journal_and_partial_checkpoint() -> None:
    alert = _alert()
    item = event("attempt", "ATTEMPT", 0)
    after_journal = recover(checkpoint(new_store(alert), item, "AFTER_JOURNAL"))
    after_primary = recover(checkpoint(new_store(alert), item, "AFTER_PRIMARY"))
    for result in (after_journal, after_primary):
        assert result["verdict"] == "PASS"
        assert result["snapshot"]["delivery_state"]["attempt"] == 1
        assert len(result["snapshot"]["delivery_state"]["processed_events"]) == 1


def test_one_torn_replica_recovers_but_both_torn_refuse() -> None:
    store = new_store(_alert())
    store["primary"]["outbox_status"] = "TORN"
    assert recover(store)["verdict"] == "PASS"
    store["secondary"]["outbox_status"] = "TORN"
    assert recover(store)["verdict"] == "REFUSE"
    assert "NO_VALID_CHECKPOINT" in recover(store)["errors"]


def test_stale_replica_uses_newer_valid_checkpoint() -> None:
    store = checkpoint(new_store(_alert()), event("attempt", "ATTEMPT", 0), "AFTER_PRIMARY")
    result = recover(store)
    assert result["verdict"] == "PASS"
    assert result["snapshot"]["generation"] == 1
    assert result["snapshot"]["delivery_state"]["attempt"] == 1


def test_corrupt_journal_and_outbox_state_divergence_refuse() -> None:
    alert = _alert()
    store = checkpoint(new_store(alert), event("attempt", "ATTEMPT", 0), "AFTER_JOURNAL")
    store["journal"]["base_generation"] = 99
    assert recover(store)["verdict"] == "REFUSE"
    divergent = new_store(alert)
    for slot in ("primary", "secondary"):
        divergent[slot]["outbox_status"] = "ACKED"
    assert recover(divergent)["verdict"] == "REFUSE"


def test_same_generation_concurrent_writer_divergence_refuses() -> None:
    alert = _alert()
    store = checkpoint(new_store(alert), event("one", "ATTEMPT", 0))
    conflicting = checkpoint(new_store(alert), event("two", "ATTEMPT", 0))["primary"]
    store["secondary"] = copy.deepcopy(conflicting)
    result = recover(store)
    assert result["verdict"] == "REFUSE"
    assert "SAME_GENERATION_DIVERGENCE" in result["errors"]


def test_duplicate_recovery_and_reordered_record_fail_safely() -> None:
    alert = _alert()
    item = event("attempt", "ATTEMPT", 0)
    store = checkpoint(new_store(alert), item, "AFTER_PRIMARY")
    assert recover(store) == recover(store)
    reordered = checkpoint(store, event("ack", "ACK", 0, attempt_id="wrong"))
    result = recover(reordered)
    assert result["verdict"] == "PASS"
    assert result["snapshot"]["delivery_state"]["status"] == "REFUSED"
    assert result["snapshot"]["critical_alert_preserved"] is True


def test_safety_invariants_are_offline() -> None:
    result = certify_crash_windows(_alert())
    assert result["real_notifications_sent"] == 0
    assert all(value is False for key, value in result["safety"].items() if key != "offline_only")
