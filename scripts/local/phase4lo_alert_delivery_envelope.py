"""Create and validate inert, hash-bound alert delivery envelopes."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime

SCHEMA = "phase4lo.alert-delivery-envelope.v1"
DECISION_SCHEMA = "phase4ln.alert-state-contract.v1"
TEMPLATE_VERSION = "runtime-alert.v1"
ROUTES = {
    "local_ui": {"destination": "runtime-alert-panel", "transport": "none"},
    "local_audit": {"destination": "runtime-alert-audit", "transport": "none"},
}
MAX_TTL_SECONDS = 900
ENVELOPE_FIELDS = {
    "schema",
    "decision_sha256",
    "alert_key",
    "severity",
    "route",
    "destination",
    "transport",
    "template_version",
    "created_at",
    "expires_at",
    "payload",
    "safety",
    "envelope_sha256",
}
PAYLOAD_FIELDS = {"title", "summary", "recommended_action"}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _decision_errors(decision: object) -> list[str]:
    if not isinstance(decision, dict):
        return ["DECISION_NOT_AN_OBJECT"]
    errors: list[str] = []
    if decision.get("schema") != DECISION_SCHEMA:
        errors.append("DECISION_BAD_SCHEMA")
    body = {key: value for key, value in decision.items() if key != "decision_sha256"}
    if decision.get("decision_sha256") != _digest(body):
        errors.append("DECISION_HASH_MISMATCH")
    if decision.get("verdict") != "PASS":
        errors.append("DECISION_NOT_PASSING")
    if decision.get("decision") != "EMIT":
        errors.append("DECISION_NOT_EMIT")
    return errors


def create_envelope(
    decision: object,
    *,
    route: str,
    created_at: str,
    expires_at: str,
    payload: object,
) -> dict[str, object]:
    errors = _decision_errors(decision)
    created = _time(created_at)
    expires = _time(expires_at)
    if route not in ROUTES:
        errors.append("ROUTE_NOT_ALLOWLISTED")
    if created is None or expires is None:
        errors.append("TIME_INVALID")
    elif expires <= created or (expires - created).total_seconds() > MAX_TTL_SECONDS:
        errors.append("TTL_INVALID")
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS:
        errors.append("PAYLOAD_FIELD_ENVELOPE_INVALID")
    elif not all(isinstance(value, str) and 0 < len(value) <= 500 for value in payload.values()):
        errors.append("PAYLOAD_VALUE_INVALID")
    source = decision if isinstance(decision, dict) else {}
    route_config = ROUTES.get(route, {"destination": None, "transport": "none"})
    envelope: dict[str, object] = {
        "schema": SCHEMA,
        "decision_sha256": source.get("decision_sha256"),
        "alert_key": source.get("alert_key"),
        "severity": source.get("severity"),
        "route": route,
        "destination": route_config["destination"],
        "transport": route_config["transport"],
        "template_version": TEMPLATE_VERSION,
        "created_at": created_at,
        "expires_at": expires_at,
        "payload": payload,
        "safety": {
            "validation_only": True,
            "delivery_enabled": False,
            "network_access": False,
            "state_write": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    envelope["envelope_sha256"] = _digest(envelope)
    return {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "envelope": envelope if not errors else None,
    }


def validate_envelope(
    envelope: object,
    decision: object,
    *,
    evaluated_at: str,
    seen_hashes: object,
) -> dict[str, object]:
    errors = _decision_errors(decision)
    now = _time(evaluated_at)
    if now is None:
        errors.append("EVALUATION_TIME_INVALID")
    seen = seen_hashes if isinstance(seen_hashes, list) else []
    if not isinstance(seen_hashes, list) or not all(isinstance(item, str) for item in seen):
        errors.append("SEEN_HASHES_INVALID")
    source = decision if isinstance(decision, dict) else {}
    if not isinstance(envelope, dict):
        errors.append("ENVELOPE_NOT_AN_OBJECT")
    else:
        if set(envelope) != ENVELOPE_FIELDS:
            errors.append("ENVELOPE_FIELD_SET_INVALID")
        if envelope.get("schema") != SCHEMA:
            errors.append("ENVELOPE_SCHEMA_INVALID")
        body = {key: value for key, value in envelope.items() if key != "envelope_sha256"}
        envelope_hash = envelope.get("envelope_sha256")
        if envelope_hash != _digest(body):
            errors.append("ENVELOPE_HASH_MISMATCH")
        if envelope_hash in seen:
            errors.append("DUPLICATE_ENVELOPE")
        if envelope.get("decision_sha256") != source.get("decision_sha256"):
            errors.append("DECISION_BINDING_MISMATCH")
        if envelope.get("alert_key") != source.get("alert_key"):
            errors.append("ALERT_KEY_MISMATCH")
        if envelope.get("severity") != source.get("severity"):
            errors.append("SEVERITY_MISMATCH")
        route = envelope.get("route")
        if route not in ROUTES:
            errors.append("ROUTE_NOT_ALLOWLISTED")
        elif any(envelope.get(key) != value for key, value in ROUTES[route].items()):
            errors.append("ROUTE_BINDING_MISMATCH")
        if envelope.get("template_version") != TEMPLATE_VERSION:
            errors.append("TEMPLATE_VERSION_INVALID")
        created = _time(envelope.get("created_at"))
        expires = _time(envelope.get("expires_at"))
        if created is None or expires is None or now is None:
            errors.append("TIME_INVALID")
        elif created > now:
            errors.append("ENVELOPE_FROM_FUTURE")
        elif now >= expires:
            errors.append("ENVELOPE_EXPIRED")
        elif expires <= created or (expires - created).total_seconds() > MAX_TTL_SECONDS:
            errors.append("TTL_INVALID")
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS:
            errors.append("PAYLOAD_FIELD_ENVELOPE_INVALID")
        elif not all(
            isinstance(value, str) and 0 < len(value) <= 500 for value in payload.values()
        ):
            errors.append("PAYLOAD_VALUE_INVALID")
        safety = envelope.get("safety")
        expected_safety = {
            "validation_only": True,
            "delivery_enabled": False,
            "network_access": False,
            "state_write": False,
            "service_control": False,
            "order_capability": False,
        }
        if safety != expected_safety:
            errors.append("DELIVERY_SAFETY_INVALID")
    result: dict[str, object] = {
        "schema": "phase4lo.alert-delivery-envelope-validation.v1",
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "envelope_sha256": envelope.get("envelope_sha256") if isinstance(envelope, dict) else None,
        "safety": {
            "validation_only": True,
            "delivery_enabled": False,
            "network_access": False,
            "state_write": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["validation_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("decision")
    parser.add_argument("payload")
    parser.add_argument("--route", required=True, choices=sorted(ROUTES))
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--expires-at", required=True)
    args = parser.parse_args()
    with open(args.decision, encoding="utf-8") as stream:
        decision = json.load(stream)
    with open(args.payload, encoding="utf-8") as stream:
        payload = json.load(stream)
    result = create_envelope(
        decision,
        route=args.route,
        created_at=args.created_at,
        expires_at=args.expires_at,
        payload=payload,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
