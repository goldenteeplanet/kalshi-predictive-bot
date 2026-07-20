from kalshi_predictor.ui.notification_delivery_simulator import simulate_local_delivery


NOW = "2026-07-18T09:10:00Z"


def _decision(incident_id, severity="WARNING", channels=None, action="DELIVER_NOW"):
    return {"incident_id":incident_id,"severity":severity,"channels":channels or ["local_desktop"],"action":action}


def _receipt(index, channel="local_desktop", delivered_at="2026-07-18T09:09:30Z"):
    return {"receipt_id":str(index),"incident_id":f"old-{index}","severity":"WARNING","channel":channel,"status":"SIMULATED_DELIVERED","delivered_at":delivered_at,"simulated":True,"external_side_effect":False}


def test_ui_obs2f_simulates_local_delivery_without_side_effects():
    report = simulate_local_delivery({"decisions":[_decision("one",channels=["local_desktop","dashboard"])]}, delivered_at=NOW)
    assert report["summary"]["simulated_delivered"] == 2
    assert all(row["simulated"] and not row["external_side_effect"] for row in report["new_receipts"])
    assert report["actual_notifications_sent"] == 0
    assert report["actual_audio_played"] is False
    assert report["network_access"] is False


def test_ui_obs2f_enforces_per_minute_and_hour_limits_for_noncritical():
    minute_prior = [_receipt(i) for i in range(3)]
    report = simulate_local_delivery({"decisions":[_decision("minute-limited")]}, delivered_at=NOW, prior_receipts=minute_prior)
    assert report["new_receipts"][0]["status"] == "SIMULATED_RATE_LIMITED"

    hour_prior = [_receipt(i, delivered_at="2026-07-18T08:30:00Z") for i in range(20)]
    report = simulate_local_delivery({"decisions":[_decision("hour-limited")]}, delivered_at=NOW, prior_receipts=hour_prior)
    assert report["new_receipts"][0]["status"] == "SIMULATED_RATE_LIMITED"


def test_ui_obs2f_critical_remains_deliverable_when_limits_are_saturated():
    prior = [_receipt(i) for i in range(25)]
    report = simulate_local_delivery({"decisions":[_decision("critical","CRITICAL",["local_audible","local_desktop","dashboard"])]}, delivered_at=NOW, prior_receipts=prior)
    assert all(row["status"] == "SIMULATED_DELIVERED" for row in report["new_receipts"])
    desktop = next(row for row in report["new_receipts"] if row["channel"] == "local_desktop")
    assert desktop["reason"] == "CRITICAL_RATE_LIMIT_BYPASS"
    assert report["summary"]["all_critical_deliverable"] is True


def test_ui_obs2f_retains_only_bounded_newest_receipts():
    prior = [_receipt(i, delivered_at="2026-07-18T08:00:00Z") for i in range(12)]
    report = simulate_local_delivery(
        {"decisions":[_decision("new",channels=["dashboard"])]}, delivered_at=NOW,
        prior_receipts=prior, receipt_limit=10,
    )
    assert len(report["retained_receipts"]) == 10
    assert report["retained_receipts"][-1]["incident_id"] == "new"
    assert report["retained_receipts"][0]["incident_id"] == "old-3"


def test_ui_obs2f_rejects_nonlocal_channels():
    report = simulate_local_delivery({"decisions":[_decision("bad",channels=["email","local_desktop"])]}, delivered_at=NOW)
    assert "NONLOCAL_CHANNEL_REJECTED:bad:email" in report["diagnostics"]
    assert [row["channel"] for row in report["new_receipts"]] == ["local_desktop"]
    assert report["external_services_contacted"] is False


def test_ui_obs2f_is_deterministic_and_database_free():
    routing = {"decisions":[_decision("one"),_decision("two","CRITICAL",["local_audible"])]}
    first = simulate_local_delivery(routing, delivered_at=NOW)
    second = simulate_local_delivery(routing, delivered_at=NOW)
    assert first == second
    assert first["database_writes"] == 0
    assert first["execution_changed"] is False
