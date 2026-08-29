"""Build a deterministic append-only runtime observation and recurrence ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from typing import Any

SCHEMA = "phase4lm.runtime-observation-ledger.v1"
CLASSIFICATION_SCHEMA = "phase4ll.runtime-snapshot-drift-classification.v1"
EVENT_KINDS = {"none", "ui_listener_unavailable", "service_stopped", "wsl_unresponsive"}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def build_ledger(observations: object) -> dict[str, object]:
    errors: list[str] = []
    accepted: list[dict[str, Any]] = []
    if not isinstance(observations, list):
        observations = []
        errors.append("OBSERVATIONS_NOT_A_LIST")
    for index, item in enumerate(observations):
        if not isinstance(item, dict):
            errors.append(f"OBSERVATION_{index}:NOT_AN_OBJECT")
            continue
        classification = item.get("classification")
        observed_at = item.get("observed_at")
        if _parse_time(observed_at) is None:
            errors.append(f"OBSERVATION_{index}:BAD_TIME")
            continue
        if not isinstance(classification, dict):
            errors.append(f"OBSERVATION_{index}:BAD_CLASSIFICATION")
            continue
        if classification.get("schema") != CLASSIFICATION_SCHEMA:
            errors.append(f"OBSERVATION_{index}:BAD_SCHEMA")
            continue
        claimed_hash = classification.get("classification_sha256")
        body = {
            key: value for key, value in classification.items() if key != "classification_sha256"
        }
        if claimed_hash != _digest(body):
            errors.append(f"OBSERVATION_{index}:HASH_MISMATCH")
            continue
        event = classification.get("event", {})
        kind = event.get("kind") if isinstance(event, dict) else None
        if kind not in EVENT_KINDS:
            errors.append(f"OBSERVATION_{index}:BAD_EVENT_KIND")
            continue
        accepted.append(
            {
                "observed_at": observed_at,
                "event_kind": kind,
                "severity": classification.get("severity"),
                "action": classification.get("action"),
                "snapshot_sha256": classification.get("candidate_snapshot_sha256"),
                "classification_sha256": claimed_hash,
            }
        )

    accepted.sort(key=lambda row: (row["observed_at"], row["classification_sha256"]))
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in accepted:
        identity = _digest(row)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)

    previous_kind = "none"
    consecutive = 0
    rolling: dict[str, int] = {kind: 0 for kind in sorted(EVENT_KINDS - {"none"})}
    chain = "0" * 64
    records: list[dict[str, object]] = []
    for sequence, row in enumerate(unique, 1):
        kind = row["event_kind"]
        consecutive = (
            consecutive + 1 if kind != "none" and kind == previous_kind else int(kind != "none")
        )
        previous_kind = kind
        if kind != "none":
            rolling[kind] += 1
        record = {
            "sequence": sequence,
            **row,
            "consecutive_recurrence": consecutive,
            "rolling_recurrence": rolling.get(kind, 0),
            "previous_record_sha256": chain,
        }
        chain = _digest(record)
        record["record_sha256"] = chain
        records.append(record)

    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "records": records,
        "summary": {
            "input_count": len(observations),
            "accepted_count": len(accepted),
            "deduplicated_count": len(unique),
            "rolling_recurrence": rolling,
            "chain_head_sha256": chain,
        },
        "safety": {
            "append_only_model": True,
            "database_write": False,
            "network_access": False,
            "service_control": False,
            "wsl_control": False,
            "order_capability": False,
        },
    }
    result["ledger_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("observations")
    args = parser.parse_args()
    with open(args.observations, encoding="utf-8") as stream:
        result = build_ledger(json.load(stream))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
