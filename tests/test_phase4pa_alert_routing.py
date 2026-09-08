from __future__ import annotations

import copy

from scripts.local.phase4pa_alert_routing import (
    ROUTES,
    aggregate_alerts,
    build_alert,
    certify_routing_coverage,
)


def test_every_restart_fault_case_has_a_dry_run_alert_route() -> None:
    result = certify_routing_coverage()
    assert result["verdict"] == "PASS"
    assert result["fault_case_count"] == result["alert_count"] == 76
    assert result["zero_unmapped_codes"] is True
    assert set(result["mapped_refusal_codes"]) == set(ROUTES)
    assert result["real_notifications_sent"] == 0


def test_invariant_violation_failed_recovery_and_restart_repetition_are_critical() -> None:
    invariant = build_alert(
        refusal_code="EXECUTION_INVARIANT_VIOLATION",
        stage="SYSTEMD_READY",
        occurrence=1,
        recovery_succeeded=True,
    )
    assert invariant["severity"] == "CRITICAL"
    assert invariant["escalation_deadline_minutes"] == 0
    failed = build_alert(
        refusal_code="STARTUP_ORDER_INVALID",
        stage="SYSTEMD_READY",
        occurrence=1,
        recovery_succeeded=False,
    )
    assert failed["severity"] == "CRITICAL"
    assert failed["failed_recovery_alert"] is True
    repeated = build_alert(
        refusal_code="SEQUENCE_INVALID",
        stage="UBUNTU_RELAUNCHED",
        occurrence=3,
        recovery_succeeded=True,
    )
    assert repeated["repeated_restart_escalation"] is True
    assert repeated["severity"] == "CRITICAL"


def test_deduplication_groups_without_suppressing_constituent_alerts() -> None:
    alerts = [
        build_alert(
            refusal_code="BOT_SERVICE_MISSING",
            stage="BOT_ACTIVE",
            occurrence=index,
            recovery_succeeded=False,
        )
        for index in (1, 2, 3)
    ]
    result = aggregate_alerts(alerts)
    assert result["verdict"] == "PASS"
    assert result["alert_count"] == 3
    assert result["groups"][0]["occurrence_count"] == 3
    assert result["groups"][0]["suppressed_count"] == 0
    assert len(result["constituent_alert_sha256s"]) == 3


def test_unknown_muted_malformed_contradictory_or_real_delivery_refuses() -> None:
    unknown = build_alert(
        refusal_code="UNKNOWN",
        stage="SYSTEMD_READY",
        occurrence=1,
        recovery_succeeded=False,
    )
    assert unknown["verdict"] == "REFUSE"
    good = build_alert(
        refusal_code="STALE_CHECKPOINT",
        stage="CHECKPOINT_RESTORED",
        occurrence=1,
        recovery_succeeded=False,
    )
    variants = []
    muted = copy.deepcopy(good)
    muted["muted"] = True
    variants.append(muted)
    invisible = copy.deepcopy(good)
    invisible["user_visible_message"] = ""
    variants.append(invisible)
    delivered = copy.deepcopy(good)
    delivered["notification_sent"] = True
    variants.append(delivered)
    contradictory = copy.deepcopy(good)
    contradictory["severity"] = "LOW"
    variants.append(contradictory)
    for alert in variants:
        assert aggregate_alerts([alert])["verdict"] == "REFUSE"


def test_alerts_are_deterministic_and_have_stable_deduplication_keys() -> None:
    kwargs = {
        "refusal_code": "RESTART_LOOP_LIMIT_EXCEEDED",
        "stage": "UBUNTU_RELAUNCHED",
        "occurrence": 3,
        "recovery_succeeded": False,
    }
    first = build_alert(**kwargs)
    assert first == build_alert(**kwargs)
    assert first["deduplication_key"] == (
        "wsl-restart:RESTART_LOOP_LIMIT_EXCEEDED:UBUNTU_RELAUNCHED"
    )
    assert first["notification_sent"] is False
    assert first["delivery_mode"] == "DRY_RUN"
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
