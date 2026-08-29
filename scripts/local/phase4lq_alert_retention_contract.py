"""Evaluate alert-lifecycle retention with privacy-minimized provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

SCHEMA = "phase4lq.alert-retention-contract.v1"
LIFECYCLE_SCHEMA = "phase4lp.alert-lifecycle.v1"
LIFECYCLE_FIELDS = {
    "schema",
    "verdict",
    "errors",
    "envelope_sha256",
    "current_state",
    "terminal",
    "records",
    "replay",
    "safety",
    "lifecycle_sha256",
}
RECORD_FIELDS = {
    "sequence",
    "transition_id",
    "envelope_sha256",
    "from_state",
    "to_state",
    "occurred_at",
    "reason",
    "previous_record_sha256",
    "record_sha256",
}
RETENTION_SECONDS = {
    "CREATED": 86_400,
    "VALIDATED": 604_800,
    "PRESENTED": 2_592_000,
    "ACKNOWLEDGED": 7_776_000,
    "EXPIRED": 2_592_000,
    "REJECTED": 2_592_000,
    "ARCHIVED": 7_776_000,
}
HOLD_REASONS = {"incident_review", "compliance_review"}
MAX_HOLD_SECONDS = 2_592_000
SENSITIVE_KEY = re.compile(
    r"(?:secret|token|password|credential|api[_-]?key|email|phone|address)", re.I
)
SENSITIVE_VALUE = re.compile(r"(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|-----BEGIN [A-Z ]+ KEY-----)")


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


def _has_sensitive_data(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            SENSITIVE_KEY.search(str(key)) or _has_sensitive_data(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_sensitive_data(item) for item in value)
    return isinstance(value, str) and SENSITIVE_VALUE.search(value) is not None


def evaluate_retention(
    lifecycle: object,
    hold: object | None,
    *,
    evaluated_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(lifecycle, dict):
        errors.append("LIFECYCLE_NOT_AN_OBJECT")
        lifecycle = {}
    elif set(lifecycle) != LIFECYCLE_FIELDS:
        errors.append("LIFECYCLE_FIELD_SET_INVALID")
    if lifecycle.get("schema") != LIFECYCLE_SCHEMA:
        errors.append("LIFECYCLE_BAD_SCHEMA")
    body = {key: value for key, value in lifecycle.items() if key != "lifecycle_sha256"}
    if lifecycle.get("lifecycle_sha256") != _digest(body):
        errors.append("LIFECYCLE_HASH_MISMATCH")
    if lifecycle.get("verdict") != "PASS":
        errors.append("LIFECYCLE_NOT_PASSING")
    if _has_sensitive_data(lifecycle):
        errors.append("SENSITIVE_DATA_REJECTED")
    now = _time(evaluated_at)
    if now is None:
        errors.append("EVALUATION_TIME_INVALID")
    records = lifecycle.get("records")
    if not isinstance(records, list) or not records:
        errors.append("RETENTION_ANCHOR_MISSING")
        records = []
    minimized: list[dict[str, object]] = []
    anchor: datetime | None = None
    chain = "0" * 64
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            errors.append(f"RECORD_{index}:FIELD_SET_INVALID")
            continue
        occurred = _time(record.get("occurred_at"))
        if occurred is None:
            errors.append(f"RECORD_{index}:TIME_INVALID")
            continue
        if record.get("sequence") != index + 1:
            errors.append(f"RECORD_{index}:SEQUENCE_INVALID")
        if record.get("envelope_sha256") != lifecycle.get("envelope_sha256"):
            errors.append(f"RECORD_{index}:ENVELOPE_BINDING_MISMATCH")
        if record.get("previous_record_sha256") != chain:
            errors.append(f"RECORD_{index}:CHAIN_LINK_INVALID")
        record_body = {key: value for key, value in record.items() if key != "record_sha256"}
        expected_record_hash = _digest(record_body)
        if record.get("record_sha256") != expected_record_hash:
            errors.append(f"RECORD_{index}:HASH_MISMATCH")
        chain = str(record.get("record_sha256"))
        anchor = occurred
        minimized.append(
            {
                "sequence": record["sequence"],
                "transition_id": record["transition_id"],
                "to_state": record["to_state"],
                "occurred_at": record["occurred_at"],
                "record_sha256": record["record_sha256"],
                "reason": "[REDACTED:NONESSENTIAL_REASON]",
            }
        )
    state = lifecycle.get("current_state")
    if records and isinstance(records[-1], dict) and records[-1].get("to_state") != state:
        errors.append("CURRENT_STATE_MISMATCH")
    replay = lifecycle.get("replay")
    if not isinstance(replay, dict) or replay.get("chain_head_sha256") != chain:
        errors.append("REPLAY_CHAIN_HEAD_MISMATCH")
    limit = RETENTION_SECONDS.get(state)
    if limit is None:
        errors.append("STATE_RETENTION_UNKNOWN")

    hold_active = False
    hold_until: datetime | None = None
    if hold is not None:
        if not isinstance(hold, dict) or set(hold) != {"reason_code", "created_at", "expires_at"}:
            errors.append("HOLD_FIELD_SET_INVALID")
        else:
            hold_created = _time(hold.get("created_at"))
            hold_until = _time(hold.get("expires_at"))
            if hold.get("reason_code") not in HOLD_REASONS:
                errors.append("HOLD_REASON_INVALID")
            if hold_created is None or hold_until is None or now is None:
                errors.append("HOLD_TIME_INVALID")
            elif (
                hold_until <= hold_created
                or (hold_until - hold_created).total_seconds() > MAX_HOLD_SECONDS
            ):
                errors.append("HOLD_WINDOW_INVALID")
            elif hold_created > now:
                errors.append("HOLD_FROM_FUTURE")
            else:
                hold_active = now < hold_until

    age_seconds = (
        int((now - anchor).total_seconds()) if now is not None and anchor is not None else None
    )
    if age_seconds is not None and age_seconds < 0:
        errors.append("LIFECYCLE_FROM_FUTURE")
    expired_by_policy = age_seconds is not None and limit is not None and age_seconds >= limit
    eligibility = "PRESERVE" if hold_active or not expired_by_policy else "EXPIRY_ELIGIBLE"
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "lifecycle_sha256": lifecycle.get("lifecycle_sha256"),
        "envelope_sha256": lifecycle.get("envelope_sha256"),
        "state": state,
        "age_seconds": age_seconds,
        "retention_limit_seconds": limit,
        "hold_active": hold_active,
        "hold_expires_at": hold.get("expires_at")
        if hold_active and isinstance(hold, dict)
        else None,
        "eligibility": eligibility if not errors else "REFUSE",
        "minimized_provenance": minimized if not errors else [],
        "redaction": {
            "nonessential_reasons_removed": len(minimized),
            "marker": "[REDACTED:NONESSENTIAL_REASON]",
            "sensitive_input_accepted": False,
        },
        "safety": {
            "advisory_only": True,
            "deletion_capability": False,
            "state_write": False,
            "network_access": False,
            "service_control": False,
            "wsl_control": False,
            "notification_delivery": False,
            "trading_authority": False,
            "order_capability": False,
        },
    }
    result["retention_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lifecycle")
    parser.add_argument("--hold")
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    with open(args.lifecycle, encoding="utf-8") as stream:
        lifecycle = json.load(stream)
    hold = None
    if args.hold:
        with open(args.hold, encoding="utf-8") as stream:
            hold = json.load(stream)
    result = evaluate_retention(lifecycle, hold, evaluated_at=args.evaluated_at)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
