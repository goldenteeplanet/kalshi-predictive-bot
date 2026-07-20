from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


LOCAL_CHANNELS = {
    "CRITICAL": ["local_audible", "local_desktop", "dashboard"],
    "HIGH": ["local_desktop", "dashboard"],
    "WARNING": ["dashboard"],
    "INFO": ["timeline"],
}
EXTERNAL_CHANNEL_TOKENS = {"email", "sms", "slack", "teams", "webhook", "push"}


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


def _quiet(now: datetime, start_hour: int, end_hour: int) -> bool:
    hour = now.hour
    return hour >= start_hour or hour < end_hour if start_hour > end_hour else start_hour <= hour < end_hour


def build_notification_routing_preview(
    incident_preview: Mapping[str, Any], policy: Mapping[str, Any],
    prior_ledger: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    timezone_name = str(policy.get("timezone") or "America/Chicago")
    timezone = ZoneInfo(timezone_name)
    now = _time(str(policy["as_of"])).astimezone(timezone)
    quiet_start = int(policy.get("quiet_start_hour", 22))
    quiet_end = int(policy.get("quiet_end_hour", 7))
    cooldown = int(policy.get("dedupe_cooldown_seconds", 1800))
    quiet_hours = _quiet(now, quiet_start, quiet_end)
    ledger = list(prior_ledger or [])
    decisions = []
    diagnostics = []
    for incident in incident_preview.get("incidents") or []:
        severity = str(incident.get("severity") or "WARNING")
        status = str(incident.get("status") or "UNRESOLVED")
        channels = list(LOCAL_CHANNELS.get(severity, ["dashboard"]))
        if any(token in channel.lower() for channel in channels for token in EXTERNAL_CHANNEL_TOKENS):
            diagnostics.append(f"EXTERNAL_CHANNEL_REJECTED:{incident.get('incident_id')}")
            channels = [channel for channel in channels if not any(token in channel.lower() for token in EXTERNAL_CHANNEL_TOKENS)]
        previous = next((item for item in reversed(ledger) if item.get("incident_id") == incident.get("incident_id")), None)
        duplicate = False
        if previous and previous.get("severity") == severity and previous.get("status") == status:
            age = int((_time(str(policy["as_of"])) - _time(str(previous["delivered_at"]))).total_seconds())
            duplicate = 0 <= age < cooldown
        critical = severity == "CRITICAL"
        resolution = status == "RESOLVED"
        if critical:
            action, reason = "DELIVER_NOW", "CRITICAL_BYPASSES_QUIET_AND_DEDUPE"
            duplicate = False
        elif duplicate:
            action, reason = "DEDUPLICATED", "WITHIN_COOLDOWN"
        elif quiet_hours:
            action, reason = "DEFER_UNTIL_QUIET_END", "NONCRITICAL_QUIET_HOURS"
        else:
            action, reason = "DELIVER_NOW", "LOCAL_ROUTE_AVAILABLE"
        if status == "OBSERVED" and severity == "INFO":
            action, reason = "TIMELINE_ONLY", "BENIGN_OBSERVED_EVENT"
        decisions.append({
            "incident_id": incident.get("incident_id"), "code": incident.get("code"),
            "severity": severity, "status": status, "acknowledged": bool(incident.get("acknowledged")),
            "action": action, "reason": reason, "channels": channels,
            "quiet_hours_active": quiet_hours, "duplicate": duplicate,
            "critical_delivery_guaranteed": critical and action == "DELIVER_NOW",
            "resolution_notification": resolution,
        })
    canonical = json.dumps(decisions, sort_keys=True, separators=(",", ":")).encode()
    critical_rows = [row for row in decisions if row["severity"] == "CRITICAL"]
    return {
        "phase": "UI-OBS-2E",
        "mode": "LOCAL_NOTIFICATION_ROUTING_QUIET_HOURS_PREVIEW",
        "external_services_contacted": False,
        "network_access": False,
        "database_access": False,
        "database_writes": 0,
        "execution_changed": False,
        "policy": {
            "timezone": timezone_name, "as_of_local": now.isoformat(),
            "quiet_start_hour": quiet_start, "quiet_end_hour": quiet_end,
            "quiet_hours_active": quiet_hours, "dedupe_cooldown_seconds": cooldown,
        },
        "decisions": decisions,
        "diagnostics": diagnostics,
        "summary": {
            "incidents": len(decisions),
            "deliver_now": sum(row["action"] == "DELIVER_NOW" for row in decisions),
            "deferred": sum(row["action"] == "DEFER_UNTIL_QUIET_END" for row in decisions),
            "deduplicated": sum(row["action"] == "DEDUPLICATED" for row in decisions),
            "timeline_only": sum(row["action"] == "TIMELINE_ONLY" for row in decisions),
            "all_critical_deliver_now": all(row["critical_delivery_guaranteed"] for row in critical_rows),
            "external_channels": 0,
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_notification_routing_preview(
    incident_path: Path, policy_path: Path, ledger_path: Path, output_dir: Path,
) -> Path:
    incident_report = json.loads(incident_path.read_text(encoding="utf-8"))
    incident_preview = incident_report.get("preview") or incident_report
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    ledger_payload = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    report = build_notification_routing_preview(incident_preview, policy, ledger_payload.get("deliveries") or [])
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "ui_obs2e_notification_routing_preview.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
