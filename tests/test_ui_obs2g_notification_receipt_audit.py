import copy

from kalshi_predictor.ui.notification_delivery_simulator import simulate_local_delivery
from kalshi_predictor.ui.notification_receipt_audit import reconcile_notification_receipts


ROUTED_AT = "2026-07-18T09:10:00Z"


def _routing():
    return {"policy":{"as_of_local":"2026-07-18T04:10:00-05:00"},"decisions":[
        {"incident_id":"critical","severity":"CRITICAL","action":"DELIVER_NOW","channels":["local_audible","local_desktop","dashboard"]},
        {"incident_id":"info","severity":"INFO","action":"TIMELINE_ONLY","channels":["timeline"]},
        {"incident_id":"deferred","severity":"HIGH","action":"DEFER_UNTIL_QUIET_END","channels":["local_desktop","dashboard"]},
    ]}


def _delivery():
    return simulate_local_delivery(_routing(), delivered_at=ROUTED_AT)


def test_ui_obs2g_proves_complete_critical_local_channel_coverage():
    report = reconcile_notification_receipts(_routing(), _delivery())
    assert report["summary"]["critical_channels_expected"] == 3
    assert report["summary"]["critical_coverage_complete"] is True
    assert report["summary"]["reconciliation_passed"] is True
    assert all(row["coverage_passed"] for row in report["rows"] if row["expected"])
    assert report["external_services_contacted"] is False


def test_ui_obs2g_detects_missing_and_duplicate_receipts():
    delivery = _delivery()
    delivery["new_receipts"] = [row for row in delivery["new_receipts"] if row["channel"] != "local_audible"]
    delivery["new_receipts"].append(copy.deepcopy(delivery["new_receipts"][0]))
    report = reconcile_notification_receipts(_routing(), delivery)
    assert "critical:local_audible" in report["findings"]["missing"]
    assert report["summary"]["duplicate"] == 1
    assert report["summary"]["critical_coverage_complete"] is False


def test_ui_obs2g_detects_late_and_unexpected_receipts():
    delivery = _delivery()
    delivery["new_receipts"][0]["delivered_at"] = "2026-07-18T09:12:00Z"
    delivery["new_receipts"].append({
        "incident_id":"unknown","severity":"WARNING","channel":"dashboard",
        "status":"SIMULATED_DELIVERED","delivered_at":ROUTED_AT,
    })
    report = reconcile_notification_receipts(_routing(), delivery, max_latency_seconds=60)
    assert report["summary"]["late"] == 1
    assert report["findings"]["unexpected"] == ["unknown:dashboard"]


def test_ui_obs2g_detects_rate_limited_expected_delivery():
    delivery = _delivery()
    desktop = next(row for row in delivery["new_receipts"] if row["channel"] == "local_desktop")
    desktop["status"] = "SIMULATED_RATE_LIMITED"
    report = reconcile_notification_receipts(_routing(), delivery)
    assert report["findings"]["rate_limited"] == ["critical:local_desktop"]
    assert report["summary"]["critical_coverage_complete"] is False


def test_ui_obs2g_deferred_decisions_expect_no_receipts():
    report = reconcile_notification_receipts(_routing(), _delivery())
    assert all(row["incident_id"] != "deferred" for row in report["rows"])


def test_ui_obs2g_is_deterministic_and_side_effect_free():
    first = reconcile_notification_receipts(_routing(), _delivery())
    second = reconcile_notification_receipts(_routing(), _delivery())
    assert first == second
    assert first["network_access"] is False
    assert first["database_writes"] == 0
    assert first["execution_changed"] is False
