"""Deterministic alert deduplication, cooldown, and acknowledgement contract."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from typing import Any

SCHEMA = "phase4ln.alert-state-contract.v1"
LEDGER_SCHEMA = "phase4lm.runtime-observation-ledger.v1"
SEVERITY_RANK = {
    "EXPECTED_TRANSIENT": 0,
    "BENIGN": 0,
    "WARNING": 1,
    "CRITICAL": 2,
    "INVALID_EVIDENCE": 3,
}
DEFAULT_COOLDOWN_SECONDS = 900
MAX_ACK_SECONDS = 86_400


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


def _validate_ledger(ledger: object) -> list[str]:
    if not isinstance(ledger, dict):
        return ["LEDGER_NOT_AN_OBJECT"]
    errors: list[str] = []
    if ledger.get("schema") != LEDGER_SCHEMA:
        errors.append("LEDGER_BAD_SCHEMA")
    claimed = ledger.get("ledger_sha256")
    body = {key: value for key, value in ledger.items() if key != "ledger_sha256"}
    if claimed != _digest(body):
        errors.append("LEDGER_HASH_MISMATCH")
    if ledger.get("verdict") != "PASS":
        errors.append("LEDGER_NOT_PASSING")
    if not isinstance(ledger.get("records"), list):
        errors.append("LEDGER_RECORDS_MALFORMED")
    return errors


def evaluate_alert(
    ledger: object,
    history: object,
    acknowledgement: object | None,
    *,
    evaluated_at: str,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
) -> dict[str, object]:
    errors = _validate_ledger(ledger)
    now = _time(evaluated_at)
    if now is None:
        errors.append("BAD_EVALUATION_TIME")
    if type(cooldown_seconds) is not int or not 0 <= cooldown_seconds <= 86_400:
        errors.append("BAD_COOLDOWN")
    history_rows = history if isinstance(history, list) else []
    if not isinstance(history, list):
        errors.append("HISTORY_NOT_A_LIST")
    records = ledger.get("records", []) if isinstance(ledger, dict) else []
    latest = records[-1] if isinstance(records, list) and records else None
    if not isinstance(latest, dict):
        errors.append("NO_CURRENT_RECORD")

    severity = latest.get("severity") if isinstance(latest, dict) else None
    if severity not in SEVERITY_RANK:
        errors.append("BAD_CURRENT_SEVERITY")
    alert_key = None
    if isinstance(latest, dict):
        alert_key = _digest(
            {
                "event_kind": latest.get("event_kind"),
                "snapshot_sha256": latest.get("snapshot_sha256"),
                "classification_sha256": latest.get("classification_sha256"),
            }
        )

    decision = "REFUSE"
    reason = "INVALID_EVIDENCE"
    last_match: dict[str, Any] | None = None
    for index, row in enumerate(history_rows):
        if not isinstance(row, dict) or _time(row.get("emitted_at")) is None:
            errors.append(f"HISTORY_{index}:MALFORMED")
            continue
        if row.get("severity") not in SEVERITY_RANK:
            errors.append(f"HISTORY_{index}:BAD_SEVERITY")
            continue
        if row.get("alert_key") == alert_key:
            if last_match is None or _time(row["emitted_at"]) > _time(last_match["emitted_at"]):
                last_match = row

    acknowledged = False
    if acknowledgement is not None:
        if not isinstance(acknowledgement, dict):
            errors.append("ACK_MALFORMED")
        else:
            acked_at = _time(acknowledgement.get("acknowledged_at"))
            expires_at = _time(acknowledgement.get("expires_at"))
            if acked_at is None or expires_at is None or now is None:
                errors.append("ACK_TIME_INVALID")
            elif (
                expires_at <= acked_at or (expires_at - acked_at).total_seconds() > MAX_ACK_SECONDS
            ):
                errors.append("ACK_WINDOW_INVALID")
            elif acknowledgement.get("alert_key") != alert_key:
                errors.append("ACK_KEY_MISMATCH")
            elif acked_at > now:
                errors.append("ACK_FROM_FUTURE")
            else:
                acknowledged = now < expires_at

    if not errors and severity in {"BENIGN", "EXPECTED_TRANSIENT"}:
        decision, reason = "SUPPRESS", "NON_ALERTING_SEVERITY"
    elif not errors and acknowledged:
        decision, reason = "SUPPRESS", "ACTIVE_ACKNOWLEDGEMENT"
    elif not errors and last_match is not None and now is not None:
        emitted = _time(last_match["emitted_at"])
        prior_rank = SEVERITY_RANK.get(last_match.get("severity"), -1)
        current_rank = SEVERITY_RANK[severity]
        elapsed = (now - emitted).total_seconds() if emitted is not None else -1
        if elapsed < 0:
            errors.append("HISTORY_FROM_FUTURE")
        elif current_rank > prior_rank:
            decision, reason = "EMIT", "SEVERITY_ESCALATED"
        elif elapsed < cooldown_seconds:
            decision, reason = "SUPPRESS", "DUPLICATE_IN_COOLDOWN"
        else:
            decision, reason = "EMIT", "COOLDOWN_EXPIRED"
    elif not errors:
        decision, reason = "EMIT", "NEW_ALERT"

    if errors:
        decision, reason = "REFUSE", "INVALID_EVIDENCE"
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "decision": decision,
        "reason": reason,
        "evaluated_at": evaluated_at,
        "alert_key": alert_key,
        "severity": severity,
        "cooldown_seconds": cooldown_seconds,
        "acknowledgement_active": acknowledged,
        "errors": sorted(set(errors)),
        "safety": {
            "recommendation_only": True,
            "notification_delivery": False,
            "state_write": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["decision_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger")
    parser.add_argument("history")
    parser.add_argument("--acknowledgement")
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--cooldown-seconds", type=int, default=DEFAULT_COOLDOWN_SECONDS)
    args = parser.parse_args()
    with open(args.ledger, encoding="utf-8") as stream:
        ledger = json.load(stream)
    with open(args.history, encoding="utf-8") as stream:
        history = json.load(stream)
    acknowledgement = None
    if args.acknowledgement:
        with open(args.acknowledgement, encoding="utf-8") as stream:
            acknowledgement = json.load(stream)
    result = evaluate_alert(
        ledger,
        history,
        acknowledgement,
        evaluated_at=args.evaluated_at,
        cooldown_seconds=args.cooldown_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
