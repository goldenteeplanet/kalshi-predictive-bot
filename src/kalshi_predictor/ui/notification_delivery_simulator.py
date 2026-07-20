from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence


SIMULATED_LOCAL_CHANNELS = {"local_audible", "local_desktop", "dashboard", "timeline"}
DEFAULT_PER_MINUTE_LIMIT = 3
DEFAULT_PER_HOUR_LIMIT = 20
DEFAULT_RECEIPT_LIMIT = 500


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


def simulate_local_delivery(
    routing: Mapping[str, Any], *, delivered_at: str,
    prior_receipts: Sequence[Mapping[str, Any]] | None = None,
    per_minute_limit: int = DEFAULT_PER_MINUTE_LIMIT,
    per_hour_limit: int = DEFAULT_PER_HOUR_LIMIT,
    receipt_limit: int = DEFAULT_RECEIPT_LIMIT,
) -> dict[str, Any]:
    if not 1 <= per_minute_limit <= 100 or not 1 <= per_hour_limit <= 1000:
        raise ValueError("invalid rate limit")
    if not 10 <= receipt_limit <= 10000:
        raise ValueError("invalid receipt retention limit")
    now = _time(delivered_at)
    receipts = [dict(item) for item in (prior_receipts or [])]
    new_receipts = []
    diagnostics = []
    for decision in routing.get("decisions") or []:
        if decision.get("action") not in {"DELIVER_NOW", "TIMELINE_ONLY"}:
            continue
        critical = decision.get("severity") == "CRITICAL"
        for channel in decision.get("channels") or []:
            if channel not in SIMULATED_LOCAL_CHANNELS:
                diagnostics.append(f"NONLOCAL_CHANNEL_REJECTED:{decision.get('incident_id')}:{channel}")
                continue
            delivered_recent_minute = sum(
                item.get("status") == "SIMULATED_DELIVERED"
                and item.get("channel") == channel
                and _time(str(item["delivered_at"])) > now - timedelta(minutes=1)
                for item in receipts + new_receipts
            )
            delivered_recent_hour = sum(
                item.get("status") == "SIMULATED_DELIVERED"
                and item.get("channel") == channel
                and _time(str(item["delivered_at"])) > now - timedelta(hours=1)
                for item in receipts + new_receipts
            )
            active_notification_channel = channel in {"local_audible", "local_desktop"}
            rate_limited = active_notification_channel and not critical and (
                delivered_recent_minute >= per_minute_limit
                or delivered_recent_hour >= per_hour_limit
            )
            status = "SIMULATED_RATE_LIMITED" if rate_limited else "SIMULATED_DELIVERED"
            reason = (
                "NONCRITICAL_RATE_LIMIT" if rate_limited
                else "CRITICAL_RATE_LIMIT_BYPASS" if active_notification_channel and critical and (
                    delivered_recent_minute >= per_minute_limit or delivered_recent_hour >= per_hour_limit
                ) else "WITHIN_RATE_LIMIT"
            )
            receipt_key = f"{decision.get('incident_id')}|{channel}|{delivered_at}|{status}"
            new_receipts.append({
                "receipt_id": hashlib.sha256(receipt_key.encode()).hexdigest()[:20],
                "incident_id": decision.get("incident_id"), "severity": decision.get("severity"),
                "channel": channel, "status": status, "reason": reason,
                "delivered_at": delivered_at, "simulated": True,
                "external_side_effect": False,
            })
    retained = (receipts + new_receipts)[-receipt_limit:]
    critical_receipts = [row for row in new_receipts if row["severity"] == "CRITICAL"]
    canonical = json.dumps(new_receipts, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "UI-OBS-2F",
        "mode": "LOCAL_DESKTOP_NOTIFICATION_DELIVERY_SIMULATOR",
        "actual_notifications_sent": 0,
        "actual_audio_played": False,
        "external_services_contacted": False,
        "network_access": False,
        "database_access": False,
        "database_writes": 0,
        "execution_changed": False,
        "policy": {
            "per_minute_per_channel": per_minute_limit,
            "per_hour_per_channel": per_hour_limit,
            "receipt_retention_limit": receipt_limit,
            "critical_bypasses_normal_limits": True,
        },
        "new_receipts": new_receipts,
        "retained_receipts": retained,
        "diagnostics": diagnostics,
        "summary": {
            "attempts": len(new_receipts),
            "simulated_delivered": sum(row["status"] == "SIMULATED_DELIVERED" for row in new_receipts),
            "simulated_rate_limited": sum(row["status"] == "SIMULATED_RATE_LIMITED" for row in new_receipts),
            "retained": len(retained),
            "all_critical_deliverable": all(row["status"] == "SIMULATED_DELIVERED" for row in critical_receipts),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_delivery_simulator_preview(
    routing_path: Path, receipts_path: Path, output_dir: Path, *, delivered_at: str,
) -> Path:
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    prior = json.loads(receipts_path.read_text(encoding="utf-8")).get("receipts", []) if receipts_path.exists() else []
    report = simulate_local_delivery(routing, delivered_at=delivered_at, prior_receipts=prior)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "ui_obs2f_local_delivery_simulator.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
