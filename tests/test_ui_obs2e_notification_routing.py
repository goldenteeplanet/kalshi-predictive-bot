import json
from pathlib import Path

from kalshi_predictor.ui.notification_routing import build_notification_routing_preview


def _incident(incident_id, severity, status="UNRESOLVED", acknowledged=False):
    return {"incident_id":incident_id,"code":incident_id.upper(),"severity":severity,"status":status,"acknowledged":acknowledged}


def _policy(as_of="2026-07-18T09:10:00Z"):
    return {"timezone":"America/Chicago","as_of":as_of,"quiet_start_hour":22,"quiet_end_hour":7,"dedupe_cooldown_seconds":1800}


def test_ui_obs2e_defers_noncritical_during_quiet_hours():
    preview = build_notification_routing_preview({"incidents":[_incident("high-1","HIGH")]}, _policy())
    row = preview["decisions"][0]
    assert preview["policy"]["quiet_hours_active"] is True
    assert row["action"] == "DEFER_UNTIL_QUIET_END"
    assert row["channels"] == ["local_desktop", "dashboard"]


def test_ui_obs2e_critical_bypasses_quiet_hours_ack_and_dedupe():
    incident = _incident("critical-1", "CRITICAL", acknowledged=True)
    ledger = [{"incident_id":"critical-1","severity":"CRITICAL","status":"UNRESOLVED","delivered_at":"2026-07-18T09:09:00Z"}]
    preview = build_notification_routing_preview({"incidents":[incident]}, _policy(), ledger)
    row = preview["decisions"][0]
    assert row["action"] == "DELIVER_NOW"
    assert row["critical_delivery_guaranteed"] is True
    assert row["duplicate"] is False
    assert "local_audible" in row["channels"]
    assert preview["summary"]["all_critical_deliver_now"] is True


def test_ui_obs2e_deduplicates_noncritical_within_cooldown_outside_quiet_hours():
    ledger = [{"incident_id":"warning-1","severity":"WARNING","status":"UNRESOLVED","delivered_at":"2026-07-18T18:55:00Z"}]
    preview = build_notification_routing_preview(
        {"incidents":[_incident("warning-1","WARNING")]}, _policy("2026-07-18T19:00:00Z"), ledger
    )
    assert preview["policy"]["quiet_hours_active"] is False
    assert preview["decisions"][0]["action"] == "DEDUPLICATED"


def test_ui_obs2e_escalated_severity_bypasses_prior_lower_severity_delivery():
    ledger = [{"incident_id":"incident-1","severity":"HIGH","status":"UNRESOLVED","delivered_at":"2026-07-18T09:09:00Z"}]
    preview = build_notification_routing_preview({"incidents":[_incident("incident-1","CRITICAL")]}, _policy(), ledger)
    assert preview["decisions"][0]["action"] == "DELIVER_NOW"
    assert preview["decisions"][0]["duplicate"] is False


def test_ui_obs2e_observed_info_is_timeline_only_and_resolution_is_local():
    preview = build_notification_routing_preview({"incidents":[
        _incident("info-1","INFO",status="OBSERVED"),
        _incident("resolved-1","WARNING",status="RESOLVED"),
    ]}, _policy("2026-07-18T19:00:00Z"))
    assert preview["decisions"][0]["action"] == "TIMELINE_ONLY"
    resolved = preview["decisions"][1]
    assert resolved["resolution_notification"] is True
    assert resolved["action"] == "DELIVER_NOW"
    assert preview["external_services_contacted"] is False
    assert preview["summary"]["external_channels"] == 0


def test_ui_obs2e_is_deterministic_and_network_free():
    incidents = {"incidents":[_incident("critical-1","CRITICAL"),_incident("high-1","HIGH")]}
    first = build_notification_routing_preview(incidents, _policy())
    second = build_notification_routing_preview(incidents, _policy())
    assert first == second
    assert first["network_access"] is False
    assert first["database_writes"] == 0
    assert first["execution_changed"] is False
