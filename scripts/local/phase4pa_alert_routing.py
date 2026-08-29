"""Restart observability saturation and dry-run alert routing proof."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4oz_restart_fault_injection import EXPECTED_CODES, run_fault_campaign

SCHEMA = "phase4pa.alert-routing.v1"
ROUTES = {
    "STARTUP_ORDER_INVALID": ("HIGH", "runtime-owner", 10),
    "SEQUENCE_INVALID": ("HIGH", "runtime-owner", 10),
    "BOT_SERVICE_MISSING": ("CRITICAL", "runtime-owner", 5),
    "STALE_CHECKPOINT": ("CRITICAL", "recovery-owner", 5),
    "EXECUTION_INVARIANT_VIOLATION": ("CRITICAL", "safety-owner", 0),
    "RESTART_LOOP_LIMIT_EXCEEDED": ("CRITICAL", "runtime-owner", 0),
    "TRANSITION_HASH_MISMATCH": ("HIGH", "observability-owner", 10),
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def build_alert(
    *, refusal_code: str, stage: str, occurrence: int, recovery_succeeded: bool
) -> dict[str, object]:
    route = ROUTES.get(refusal_code)
    errors = []
    if route is None:
        errors.append("UNKNOWN_OR_UNMAPPED_REFUSAL")
        severity, owner, deadline = "CRITICAL", "safety-owner", 0
    else:
        severity, owner, deadline = route
    if occurrence < 1:
        errors.append("OCCURRENCE_INVALID")
    repeated = occurrence >= 3
    failed_recovery = not recovery_succeeded
    if repeated or failed_recovery:
        severity, deadline = "CRITICAL", 0
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "refusal_code": refusal_code,
        "stage": stage,
        "severity": severity,
        "deduplication_key": f"wsl-restart:{refusal_code}:{stage}",
        "occurrence": occurrence,
        "repeated_restart_escalation": repeated,
        "recovery_succeeded": recovery_succeeded,
        "failed_recovery_alert": failed_recovery,
        "owner": owner,
        "escalation_deadline_minutes": deadline,
        "user_visible_message": (
            f"{severity}: WSL recovery refusal {refusal_code} at {stage}; "
            f"occurrence {occurrence}; recovery_succeeded={str(recovery_succeeded).lower()}."
        ),
        "muted": False,
        "delivery_mode": "DRY_RUN",
        "notification_sent": False,
        "safety": _safety(),
    }
    return {**body, "alert_sha256": _digest(body)}


def aggregate_alerts(alerts: list[dict[str, object]]) -> dict[str, object]:
    errors = []
    constituent_hashes = []
    for alert in alerts:
        unsigned = {key: value for key, value in alert.items() if key != "alert_sha256"}
        if alert.get("alert_sha256") != _digest(unsigned):
            errors.append("ALERT_HASH_MISMATCH")
        if alert.get("verdict") != "PASS":
            errors.append("ALERT_NOT_ROUTABLE")
        if alert.get("muted") is not False or not alert.get("user_visible_message"):
            errors.append("ALERT_MUTED_OR_INVISIBLE")
        if alert.get("notification_sent") is not False or alert.get("delivery_mode") != "DRY_RUN":
            errors.append("REAL_NOTIFICATION_ATTEMPTED")
        try:
            expected = build_alert(
                refusal_code=str(alert["refusal_code"]),
                stage=str(alert["stage"]),
                occurrence=int(alert["occurrence"]),
                recovery_succeeded=bool(alert["recovery_succeeded"]),
            )
        except (KeyError, TypeError, ValueError):
            errors.append("ALERT_FIELDS_MALFORMED")
        else:
            if alert != expected:
                errors.append("ALERT_ROUTE_CONTRADICTORY")
        constituent_hashes.append(alert.get("alert_sha256"))
    keys = [alert.get("deduplication_key") for alert in alerts]
    grouped = []
    for key in sorted(set(keys), key=str):
        members = [alert for alert in alerts if alert.get("deduplication_key") == key]
        grouped.append(
            {
                "deduplication_key": key,
                "occurrence_count": len(members),
                "constituent_alert_sha256s": [alert["alert_sha256"] for alert in members],
                "highest_severity": (
                    "CRITICAL"
                    if any(alert["severity"] == "CRITICAL" for alert in members)
                    else "HIGH"
                ),
                "suppressed_count": 0,
            }
        )
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "alert_count": len(alerts),
        "constituent_alert_sha256s": constituent_hashes,
        "groups": grouped,
        "all_alerts_preserved": sum(row["occurrence_count"] for row in grouped) == len(alerts),
        "real_notifications_sent": 0,
        "safety": _safety(),
    }
    if not body["all_alerts_preserved"]:
        body["verdict"] = "REFUSE"
        body["errors"].append("ALERT_SUPPRESSION_DETECTED")
    return {**body, "aggregate_sha256": _digest(body)}


def certify_routing_coverage() -> dict[str, object]:
    campaign = run_fault_campaign()
    expected_codes = sorted(set(EXPECTED_CODES.values()))
    errors = []
    if sorted(ROUTES) != expected_codes:
        errors.append("ROUTING_COVERAGE_INCOMPLETE_OR_EXTRA")
    alerts = []
    for row in campaign["results"]:
        alerts.append(
            build_alert(
                refusal_code=row["expected_code_fragment"],
                stage=row["stage"],
                occurrence=1,
                recovery_succeeded=False,
            )
        )
    aggregate = aggregate_alerts(alerts)
    if aggregate["verdict"] != "PASS":
        errors.append("ALERT_AGGREGATION_FAILED")
    if len(alerts) != campaign["case_count"]:
        errors.append("FAULT_ALERT_CARDINALITY_MISMATCH")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "fault_campaign_sha256": campaign["campaign_sha256"],
        "mapped_refusal_codes": expected_codes,
        "fault_case_count": campaign["case_count"],
        "alert_count": len(alerts),
        "aggregate_alert_sha256": aggregate["aggregate_sha256"],
        "zero_unmapped_codes": not errors,
        "real_notifications_sent": 0,
        "safety": _safety(),
    }
    return {**body, "coverage_sha256": _digest(body)}


def _safety() -> dict[str, bool]:
    return {
        "offline_only": True,
        "infrastructure_mutation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
