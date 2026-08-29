"""Canonical, secret-refusing snapshot of fail-closed runtime configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "phase4lk.runtime-configuration-snapshot.v1"
SECRET_PATTERN = re.compile(
    r"(?:api[_-]?key|secret|password|credential|token|cookie|database[_-]?url)", re.I
)
LAUNCHER_FIELDS = {
    "EXECUTION_ENABLED",
    "EXECUTION_DRY_RUN",
    "EXECUTION_KILL_SWITCH",
    "DEMO_EXECUTION_ENABLED",
    "AUTOPILOT_ENABLED",
    "AUTOPILOT_DRY_RUN",
    "PAPER_ORDER_CREATION_ENABLED",
    "PAPER_ORDER_KILL_SWITCH",
    "KALSHI_REFRESH_INTERVAL_SECONDS",
}
UI_ENV_FIELDS = {
    "UI_READ_ONLY",
    "EXECUTION_ENABLED",
    "KALSHI_UI_PORT",
    "PYTHONDONTWRITEBYTECODE",
}


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _pairs(value: object, allowed: set[str], label: str, errors: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(value, list):
        errors.append(f"{label}:NOT_A_PAIR_LIST")
        return result
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(x, str) for x in item)
        ):
            errors.append(f"{label}:MALFORMED_PAIR")
            continue
        key, raw = item
        if SECRET_PATTERN.search(key):
            errors.append(f"{label}:SECRET_LIKE_FIELD")
            continue
        if key not in allowed:
            errors.append(f"{label}:UNKNOWN_FIELD:{key}")
            continue
        if key in result:
            errors.append(f"{label}:DUPLICATE_FIELD:{key}")
            continue
        result[key] = raw
    return result


def _boolean(settings: dict[str, str], key: str, errors: list[str]) -> bool | None:
    raw = settings.get(key)
    if raw == "true":
        return True
    if raw == "false":
        return False
    errors.append(f"INVALID_BOOLEAN:{key}")
    return None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == UTC else None


def _secret_scan(value: object, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_PATTERN.search(str(key)):
                errors.append(f"SECRET_LIKE_FIELD:{path}.{key}")
            errors.extend(_secret_scan(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_secret_scan(child, f"{path}[{index}]"))
    return errors


def build_snapshot(
    service_source: object,
    launcher_pairs: object,
    health_source: object,
    *,
    observed_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    errors.extend(_secret_scan(service_source))
    errors.extend(_secret_scan(launcher_pairs))
    errors.extend(_secret_scan(health_source))
    observed = _timestamp(observed_at)
    if observed is None:
        errors.append("BAD_OBSERVED_AT")

    launcher = _pairs(launcher_pairs, LAUNCHER_FIELDS, "LAUNCHER", errors)
    missing_launcher = sorted(LAUNCHER_FIELDS - set(launcher))
    errors.extend(f"LAUNCHER:MISSING_FIELD:{field}" for field in missing_launcher)
    normalized = {
        key.lower(): _boolean(launcher, key, errors)
        for key in LAUNCHER_FIELDS
        if key != "KALSHI_REFRESH_INTERVAL_SECONDS"
    }
    try:
        cadence = int(launcher.get("KALSHI_REFRESH_INTERVAL_SECONDS", ""))
    except ValueError:
        cadence = 0
    if not 60 <= cadence <= 1_800:
        errors.append("UNSAFE_REFRESH_CADENCE")
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
    for field, expected_value in expected.items():
        if normalized.get(field) is not expected_value:
            errors.append(f"UNSAFE_SETTING:{field}")

    services: dict[str, dict[str, object]] = {}
    if not isinstance(service_source, dict) or set(service_source) != {"refresh", "ui"}:
        errors.append("SERVICE_SOURCE_SHAPE_INVALID")
        service_source = {}
    for name in ("refresh", "ui"):
        raw = service_source.get(name, {}) if isinstance(service_source, dict) else {}
        allowed = {"active", "enabled", "restart", "restart_sec", "n_restarts", "environment"}
        if not isinstance(raw, dict) or set(raw) - allowed:
            errors.append(f"SERVICE_{name.upper()}:UNKNOWN_OR_MALFORMED_FIELD")
            raw = raw if isinstance(raw, dict) else {}
        if raw.get("active") is not True or raw.get("enabled") is not True:
            errors.append(f"SERVICE_{name.upper()}:NOT_ACTIVE_AND_ENABLED")
        if raw.get("restart") != "always":
            errors.append(f"SERVICE_{name.upper()}:RESTART_PROTECTION_DISABLED")
        restart_sec = raw.get("restart_sec")
        if type(restart_sec) is not int or not 1 <= restart_sec <= 60:
            errors.append(f"SERVICE_{name.upper()}:UNSAFE_RESTART_DELAY")
        environment = _pairs(
            raw.get("environment", []), UI_ENV_FIELDS, f"SERVICE_{name.upper()}_ENV", errors
        )
        if name == "ui":
            if environment.get("UI_READ_ONLY") != "true":
                errors.append("SERVICE_UI:NOT_READ_ONLY")
            if environment.get("EXECUTION_ENABLED") != "false":
                errors.append("SERVICE_UI:EXECUTION_NOT_DISABLED")
        services[name] = {
            "active": raw.get("active"),
            "enabled": raw.get("enabled"),
            "restart": raw.get("restart"),
            "restart_sec": restart_sec,
            "n_restarts": raw.get("n_restarts"),
            "ui_read_only": environment.get("UI_READ_ONLY") == "true" if name == "ui" else None,
        }

    if not isinstance(health_source, dict):
        health_source = {}
        errors.append("HEALTH_SOURCE_NOT_AN_OBJECT")
    allowed_health = {"status", "generated_at", "writer_count", "writer_exclusive"}
    if set(health_source) - allowed_health:
        errors.append("HEALTH_SOURCE_UNKNOWN_FIELD")
    generated = _timestamp(health_source.get("generated_at"))
    age_seconds = None
    if observed is None or generated is None:
        errors.append("HEALTH_TIMESTAMP_INVALID")
    else:
        age_seconds = int((observed - generated).total_seconds())
        if age_seconds < 0 or age_seconds > 1_800:
            errors.append("HEALTH_EVIDENCE_STALE")
    if health_source.get("status") != "healthy":
        errors.append("HEALTH_STATUS_NOT_HEALTHY")
    if health_source.get("writer_count") != 1 or health_source.get("writer_exclusive") is not True:
        errors.append("WRITER_EXCLUSIVITY_FAILED")

    errors = sorted(set(errors))
    snapshot: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "observed_at": observed_at,
        "settings": {**normalized, "refresh_interval_seconds": cadence},
        "services": services,
        "health": {
            "status": health_source.get("status"),
            "age_seconds": age_seconds,
            "writer_count": health_source.get("writer_count"),
            "writer_exclusive": health_source.get("writer_exclusive"),
        },
        "errors": errors,
        "redaction": {
            "allowlisted_fields_only": True,
            "secret_like_fields_rejected": True,
            "database_url_included": False,
            "credentials_included": False,
        },
        "safety": {
            "read_only": True,
            "service_control": False,
            "database_access": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    snapshot["snapshot_sha256"] = _digest(snapshot)
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--services", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--health", type=Path, required=True)
    parser.add_argument("--observed-at", required=True)
    args = parser.parse_args()
    values = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (args.services, args.launcher, args.health)
    ]
    result = build_snapshot(*values, observed_at=args.observed_at)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
