"""Phase 4AB read-only dwell and escalation monitor for Phase 4AA artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "phase4ab.settlement-closure-dwell.v1"
SOURCE_SCHEMA = "phase4aa.settlement-closure-gate.v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def artifact_hash(payload: dict[str, Any]) -> str:
    return _hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def load_gate(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SOURCE_SCHEMA:
        raise ValueError("PHASE4AB_SOURCE_SCHEMA_INVALID")
    if payload.get("artifact_hash") != artifact_hash(payload):
        raise ValueError("PHASE4AB_SOURCE_HASH_MISMATCH")
    required = {
        "generated_at",
        "state",
        "safe_to_advance",
        "source_rows_hash",
        "source_hint_artifact_hash",
        "hint_count",
        "canonical_count",
        "fully_evaluated_count",
        "blocking_counts",
    }
    if not required.issubset(payload):
        raise ValueError("PHASE4AB_SOURCE_FIELDS_MISSING")
    if payload.get("production_database_written") is not False:
        raise ValueError("PHASE4AB_SOURCE_PRODUCTION_WRITE_FLAG_INVALID")
    if payload.get("trading_mode_changed") is not False:
        raise ValueError("PHASE4AB_SOURCE_TRADING_FLAG_INVALID")
    return payload


def _progress(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    return int(current["canonical_count"]) > int(previous["canonical_count"]) or int(
        current["fully_evaluated_count"]
    ) > int(previous["fully_evaluated_count"])


def _regressed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    same_hint_lineage = (
        current["source_hint_artifact_hash"] == previous["source_hint_artifact_hash"]
    )
    return same_hint_lineage and (
        int(current["canonical_count"]) < int(previous["canonical_count"])
        or int(current["fully_evaluated_count"]) < int(previous["fully_evaluated_count"])
        or int(current["hint_count"]) != int(previous["hint_count"])
    )


def build_status(
    gates: list[dict[str, Any]],
    *,
    now: datetime,
    maximum_evidence_age_seconds: int,
    settlement_dwell_seconds: int,
    reconciliation_dwell_seconds: int,
) -> dict[str, Any]:
    if not gates:
        raise ValueError("PHASE4AB_SOURCE_HISTORY_EMPTY")
    if (
        min(
            maximum_evidence_age_seconds,
            settlement_dwell_seconds,
            reconciliation_dwell_seconds,
        )
        < 0
    ):
        raise ValueError("PHASE4AB_THRESHOLD_NEGATIVE")
    ordered = sorted(gates, key=lambda row: _utc(row["generated_at"]))
    timestamps = [_utc(row["generated_at"]) for row in ordered]
    if len(set(timestamps)) != len(timestamps):
        raise ValueError("PHASE4AB_SOURCE_TIMESTAMP_DUPLICATE")
    latest = ordered[-1]
    latest_at = timestamps[-1]
    age_seconds = (now.astimezone(UTC) - latest_at).total_seconds()
    last_progress_at = timestamps[0]
    regression = False
    progress_events = 0
    for index in range(1, len(ordered)):
        if _regressed(ordered[index - 1], ordered[index]):
            regression = True
        if _progress(ordered[index - 1], ordered[index]):
            last_progress_at = timestamps[index]
            progress_events += 1
    dwell_seconds = max(0.0, (latest_at - last_progress_at).total_seconds())
    blocking_counts = latest.get("blocking_counts")
    lineage_failure = bool(blocking_counts) or latest.get("state") == "ATTENTION"
    if age_seconds < 0:
        state, reason = "ATTENTION", "LATEST_EVIDENCE_FROM_FUTURE"
    elif regression:
        state, reason = "ATTENTION", "CLOSURE_COUNTS_REGRESSED"
    elif age_seconds >= maximum_evidence_age_seconds:
        state, reason = "STALE", "LATEST_EVIDENCE_AGE_THRESHOLD_REACHED"
    elif lineage_failure:
        state, reason = "ATTENTION", "LINEAGE_FAILURE_REQUIRES_REVIEW"
    elif latest.get("state") == "COMPLETE" and latest.get("safe_to_advance") is True:
        state, reason = "COMPLETE", "SETTLEMENT_CLOSURE_COMPLETE"
    elif latest.get("state") == "WAITING_RECONCILIATION":
        if dwell_seconds >= reconciliation_dwell_seconds:
            state, reason = "ESCALATE", "RECONCILIATION_DWELL_THRESHOLD_REACHED"
        else:
            state, reason = "WAITING", "RECONCILIATION_WITHIN_DWELL_BUDGET"
    elif latest.get("state") in {"WAITING_SETTLEMENT", "WAITING_NO_HINTS"}:
        if dwell_seconds >= settlement_dwell_seconds:
            state, reason = "ESCALATE", "SETTLEMENT_DWELL_THRESHOLD_REACHED"
        else:
            state, reason = "WAITING", "SETTLEMENT_WITHIN_DWELL_BUDGET"
    else:
        state, reason = "ATTENTION", "UNKNOWN_SOURCE_STATE"
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": now.astimezone(UTC).isoformat(),
        "state": state,
        "reason": reason,
        "safe_to_advance": state == "COMPLETE",
        "production_database_written": False,
        "trading_mode_changed": False,
        "history_count": len(ordered),
        "history_hash": _hash([row["artifact_hash"] for row in ordered]),
        "latest_gate_hash": latest["artifact_hash"],
        "latest_gate_state": latest["state"],
        "latest_generated_at": latest_at.isoformat(),
        "latest_evidence_age_seconds": age_seconds,
        "last_progress_at": last_progress_at.isoformat(),
        "dwell_seconds": dwell_seconds,
        "progress_events": progress_events,
        "thresholds": {
            "maximum_evidence_age_seconds": maximum_evidence_age_seconds,
            "settlement_dwell_seconds": settlement_dwell_seconds,
            "reconciliation_dwell_seconds": reconciliation_dwell_seconds,
        },
        "latest_counts": {
            "hint_count": int(latest["hint_count"]),
            "canonical_count": int(latest["canonical_count"]),
            "fully_evaluated_count": int(latest["fully_evaluated_count"]),
        },
    }
    payload["artifact_hash"] = artifact_hash(payload)
    return payload


def invalid_source_status(*, now: datetime, error: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": now.astimezone(UTC).isoformat(),
        "state": "ATTENTION",
        "reason": "SOURCE_VALIDATION_FAILED",
        "error_code": str(error),
        "safe_to_advance": False,
        "production_database_written": False,
        "trading_mode_changed": False,
    }
    payload["artifact_hash"] = artifact_hash(payload)
    return payload


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-artifact", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-evidence-age-seconds", type=int, default=1800)
    parser.add_argument("--settlement-dwell-seconds", type=int, default=86400)
    parser.add_argument("--reconciliation-dwell-seconds", type=int, default=3600)
    args = parser.parse_args()
    now = datetime.now(UTC)
    try:
        gates = [load_gate(path) for path in args.gate_artifact]
        payload = build_status(
            gates,
            now=now,
            maximum_evidence_age_seconds=args.maximum_evidence_age_seconds,
            settlement_dwell_seconds=args.settlement_dwell_seconds,
            reconciliation_dwell_seconds=args.reconciliation_dwell_seconds,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        payload = invalid_source_status(now=now, error=error)
    write_atomic(args.output, payload)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
