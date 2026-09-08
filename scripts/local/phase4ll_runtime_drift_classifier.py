"""Deterministic drift severity and escalation policy for Phase 4LK snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

SCHEMA = "phase4ll.runtime-snapshot-drift-classification.v1"
SNAPSHOT_SCHEMA = "phase4lk.runtime-configuration-snapshot.v1"
POLICY_VERSION = "phase4ll.escalation-policy.v1"
SEVERITIES = ("EXPECTED_TRANSIENT", "BENIGN", "WARNING", "CRITICAL", "INVALID_EVIDENCE")
TOP_LEVEL_FIELDS = {
    "schema",
    "verdict",
    "observed_at",
    "settings",
    "services",
    "health",
    "errors",
    "redaction",
    "safety",
    "snapshot_sha256",
}


def _digest(payload: dict[str, Any], field: str | None = None) -> str:
    body = {key: value for key, value in payload.items() if key != field} if field else payload
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _snapshot_errors(snapshot: object, label: str) -> list[str]:
    if not isinstance(snapshot, dict):
        return [f"{label}:NOT_AN_OBJECT"]
    errors: list[str] = []
    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        errors.append(f"{label}:BAD_SCHEMA")
    if set(snapshot) != TOP_LEVEL_FIELDS:
        errors.append(f"{label}:FIELD_ENVELOPE_MISMATCH")
    if snapshot.get("snapshot_sha256") != _digest(snapshot, "snapshot_sha256"):
        errors.append(f"{label}:HASH_MISMATCH")
    redaction = snapshot.get("redaction")
    if not isinstance(redaction, dict) or redaction.get("secret_like_fields_rejected") is not True:
        errors.append(f"{label}:REDACTION_NOT_PROVED")
    return errors


def _flatten(value: object, prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    if isinstance(value, dict):
        for key in sorted(value):
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(value[key], path))
    else:
        result[prefix] = value
    return result


def _unsafe_reasons(snapshot: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    settings = snapshot.get("settings", {})
    expected = {
        "execution_enabled": False,
        "execution_dry_run": True,
        "execution_kill_switch": True,
        "demo_execution_enabled": False,
        "autopilot_enabled": False,
        "autopilot_dry_run": True,
        "paper_order_creation_enabled": False,
        "paper_order_kill_switch": True,
    }
    if not isinstance(settings, dict):
        return ["SETTINGS_MALFORMED"]
    for key, safe in expected.items():
        if settings.get(key) is not safe:
            reasons.append(f"UNSAFE_SETTING:{key}")
    cadence = settings.get("refresh_interval_seconds")
    if type(cadence) is not int or not 60 <= cadence <= 1_800:
        reasons.append("UNSAFE_REFRESH_CADENCE")
    services = snapshot.get("services", {})
    for name in ("refresh", "ui"):
        service = services.get(name, {}) if isinstance(services, dict) else {}
        if service.get("active") is not True or service.get("enabled") is not True:
            reasons.append(f"SERVICE_UNAVAILABLE:{name}")
        if service.get("restart") != "always":
            reasons.append(f"RESTART_PROTECTION_DISABLED:{name}")
    ui = services.get("ui", {}) if isinstance(services, dict) else {}
    if ui.get("ui_read_only") is not True:
        reasons.append("UI_NOT_READ_ONLY")
    health = snapshot.get("health", {})
    if not isinstance(health, dict):
        reasons.append("HEALTH_MALFORMED")
    else:
        age_seconds = health.get("age_seconds")
        if health.get("status") != "healthy" or type(age_seconds) is not int or age_seconds > 1_800:
            reasons.append("HEALTH_UNSAFE_OR_STALE")
        if health.get("writer_count") != 1 or health.get("writer_exclusive") is not True:
            reasons.append("WRITER_EXCLUSIVITY_LOST")
    return sorted(set(reasons))


def classify_drift(
    baseline: object,
    candidate: object,
    *,
    event_kind: str = "none",
    recurrence_count: int = 0,
    duration_seconds: int = 0,
) -> dict[str, object]:
    evidence_errors = _snapshot_errors(baseline, "BASELINE")
    evidence_errors.extend(_snapshot_errors(candidate, "CANDIDATE"))
    valid_events = {"none", "ui_listener_unavailable", "service_stopped", "wsl_unresponsive"}
    if event_kind not in valid_events or recurrence_count < 0 or duration_seconds < 0:
        evidence_errors.append("EVENT_EVIDENCE_INVALID")
    base = baseline if isinstance(baseline, dict) else {}
    current = candidate if isinstance(candidate, dict) else {}
    base_flat = _flatten(base)
    current_flat = _flatten(current)
    ignored = {"snapshot_sha256", "observed_at"}
    drift = [
        {"field": field, "before": base_flat.get(field), "after": current_flat.get(field)}
        for field in sorted(set(base_flat) | set(current_flat))
        if field not in ignored and base_flat.get(field) != current_flat.get(field)
    ]
    unsafe = _unsafe_reasons(current) if not evidence_errors else []

    if evidence_errors:
        severity, action = "INVALID_EVIDENCE", "HUMAN_INTERVENTION_REQUIRED"
    elif unsafe:
        severity = "CRITICAL"
        if (
            any(reason.startswith("SERVICE_UNAVAILABLE:") for reason in unsafe)
            and recurrence_count < 3
        ):
            action = "RESTART_SERVICE_ELIGIBLE"
        else:
            action = "HUMAN_INTERVENTION_REQUIRED"
    elif event_kind == "wsl_unresponsive":
        severity = "CRITICAL"
        action = "RESTART_WSL_ELIGIBLE" if recurrence_count <= 1 else "HUMAN_INTERVENTION_REQUIRED"
    elif event_kind in {"ui_listener_unavailable", "service_stopped"}:
        if (
            event_kind == "ui_listener_unavailable"
            and duration_seconds <= 30
            and recurrence_count <= 1
        ):
            severity, action = "EXPECTED_TRANSIENT", "MONITOR"
        elif recurrence_count < 3:
            severity, action = "WARNING", "RESTART_SERVICE_ELIGIBLE"
        else:
            severity, action = "CRITICAL", "HUMAN_INTERVENTION_REQUIRED"
    else:
        changed_fields = {row["field"] for row in drift}
        restart_changes = {field for field in changed_fields if field.endswith(".n_restarts")}
        health_age_only = changed_fields <= {"health.age_seconds"}
        if not drift:
            severity, action = "BENIGN", "QUIET"
        elif (
            health_age_only
            and type(current.get("health", {}).get("age_seconds")) is int
            and current["health"]["age_seconds"] <= 900
        ):
            severity, action = "EXPECTED_TRANSIENT", "QUIET"
        elif changed_fields <= restart_changes | {"health.age_seconds"}:
            severity, action = "WARNING", "ALERT"
        else:
            severity, action = "WARNING", "ALERT"

    result: dict[str, object] = {
        "schema": SCHEMA,
        "policy_version": POLICY_VERSION,
        "severity": severity,
        "action": action,
        "baseline_snapshot_sha256": base.get("snapshot_sha256"),
        "candidate_snapshot_sha256": current.get("snapshot_sha256"),
        "event": {
            "kind": event_kind,
            "recurrence_count": recurrence_count,
            "duration_seconds": duration_seconds,
        },
        "drift": drift,
        "unsafe_reasons": unsafe,
        "evidence_errors": sorted(set(evidence_errors)),
        "safety": {
            "classification_only": True,
            "service_control": False,
            "wsl_control": False,
            "trading_authority": False,
            "order_capability": False,
        },
    }
    result["classification_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--event-kind", default="none")
    parser.add_argument("--recurrence-count", type=int, default=0)
    parser.add_argument("--duration-seconds", type=int, default=0)
    args = parser.parse_args()
    with open(args.baseline, encoding="utf-8") as stream:
        baseline = json.load(stream)
    with open(args.candidate, encoding="utf-8") as stream:
        candidate = json.load(stream)
    result = classify_drift(
        baseline,
        candidate,
        event_kind=args.event_kind,
        recurrence_count=args.recurrence_count,
        duration_seconds=args.duration_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["severity"] in {"BENIGN", "EXPECTED_TRANSIENT"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
