"""Audit freshness propagation through the guarded time-to-trade stages."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bt.freshness-chain-input.v1"
SCHEMA = "phase4bt.freshness-propagation-audit.v1"
VERDICT_SCHEMA = "phase4bt.freshness-propagation-verdict.v1"
STAGES = (
    "COLLECTION",
    "SNAPSHOT",
    "FORECAST",
    "RANKING",
    "POSITION_SIZING",
    "ADVANCED_RISK",
    "APPROVAL",
    "PAPER_ROUTING",
    "OBSERVABILITY",
)
MAX_FRESHNESS_AGE_SECONDS = 604_800


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("PHASE4BT_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4BT_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("PHASE4BT_TIMESTAMP_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def build(payload: dict[str, Any], *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BT_EVALUATION_TIMEZONE_MISSING")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BT_INPUT_SCHEMA_OR_HASH_INVALID")
    rows = payload.get("stages")
    if not isinstance(rows, list) or [
        row.get("stage") for row in rows if isinstance(row, dict)
    ] != list(STAGES):
        raise ValueError("PHASE4BT_STAGE_COVERAGE_OR_ORDER_INVALID")
    expected_fields = {
        "stage",
        "produced_at",
        "data_as_of",
        "max_age_seconds",
        "reported_current",
        "predecessor_hash",
        "row_hash",
    }
    normalized: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    previous_hash: str | None = None
    previous_data_as_of: datetime | None = None
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise ValueError("PHASE4BT_STAGE_FIELDS_INVALID")
        if row["row_hash"] != _hash(row, "row_hash"):
            raise ValueError("PHASE4BT_ROW_HASH_INVALID")
        if row["predecessor_hash"] != previous_hash:
            raise ValueError("PHASE4BT_LINEAGE_INVALID")
        produced, data_as_of = _time(row["produced_at"]), _time(row["data_as_of"])
        if data_as_of > produced or produced > now.astimezone(UTC):
            raise ValueError("PHASE4BT_TEMPORAL_ORDER_INVALID")
        if previous_data_as_of is not None and data_as_of < previous_data_as_of:
            raise ValueError("PHASE4BT_FRESHNESS_REGRESSION")
        maximum = row["max_age_seconds"]
        if (
            not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or not 0 <= maximum <= MAX_FRESHNESS_AGE_SECONDS
        ):
            raise ValueError("PHASE4BT_MAX_AGE_INVALID")
        if not isinstance(row["reported_current"], bool):
            raise ValueError("PHASE4BT_REPORTED_CURRENT_INVALID")
        production_age = int((produced - data_as_of).total_seconds())
        evaluation_age = int((now.astimezone(UTC) - data_as_of).total_seconds())
        production_fresh = production_age <= maximum
        evaluation_fresh = evaluation_age <= maximum
        if row["reported_current"] and not evaluation_fresh:
            findings.append({"stage": row["stage"], "finding": "STALE_BUT_REPORTED_CURRENT"})
        if not row["reported_current"] and evaluation_fresh:
            findings.append({"stage": row["stage"], "finding": "FRESH_BUT_REPORTED_STALE"})
        normalized.append(
            {
                **row,
                "production_age_seconds": production_age,
                "evaluation_age_seconds": evaluation_age,
                "production_fresh": production_fresh,
                "evaluation_fresh": evaluation_fresh,
            }
        )
        previous_hash = row["row_hash"]
        previous_data_as_of = data_as_of
    audit: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BT",
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "input_hash": payload["artifact_hash"],
        "stages": normalized,
        "terminal_row_hash": previous_hash,
        "findings": findings,
        "finding_count": len(findings),
        "production_records_created": 0,
        "execution_authorized": False,
    }
    audit["artifact_hash"] = _hash(audit)
    blocking = [
        finding for finding in findings if finding["finding"] == "STALE_BUT_REPORTED_CURRENT"
    ]
    verdict: dict[str, Any] = {
        "schema": VERDICT_SCHEMA,
        "phase": "4BT",
        "audit_hash": audit["artifact_hash"],
        "stale_current_contradictions": blocking,
        "stale_current_contradiction_count": len(blocking),
        "verdict": "FRESHNESS_PROPAGATION_VALID"
        if not findings
        else "FRESHNESS_PROPAGATION_INVALID",
        "advancement_allowed": not findings,
        "execution_authorized": False,
    }
    verdict["artifact_hash"] = _hash(verdict)
    return audit, verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freshness-input", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--verdict-output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.freshness_input.read_text(encoding="utf-8"))
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    audit, verdict = build(payload, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.audit_output, args.verdict_output, audit, verdict)
    print(json.dumps(audit, sort_keys=True))


if __name__ == "__main__":
    main()
