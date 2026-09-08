"""Attribute guarded pipeline latency to explicit deterministic cause buckets."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bs.latency-attribution-input.v1"
SCHEMA = "phase4bs.latency-attribution-report.v1"
CAUSE_SCHEMA = "phase4bs.latency-cause-ranking.v1"
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
CAUSES = (
    "COMPUTE",
    "IO",
    "RATE_LIMITING",
    "QUEUEING",
    "LOCK_WAIT",
    "STALE_EVIDENCE",
    "EXTERNAL_API",
    "OPERATOR_WAIT",
)
MAX_DURATION_MS = 86_400_000


def _hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("PHASE4BS_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4BS_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("PHASE4BS_TIMESTAMP_AMBIGUOUS")
    return parsed.astimezone(UTC)


def build(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BS_INPUT_SCHEMA_OR_HASH_INVALID")
    for field in ("baseline_hash", "budget_hash"):
        if not isinstance(payload.get(field), str) or len(payload[field]) != 64:
            raise ValueError("PHASE4BS_UPSTREAM_HASH_INVALID")
    rows = payload.get("stages")
    if not isinstance(rows, list) or [
        row.get("stage") for row in rows if isinstance(row, dict)
    ] != list(STAGES):
        raise ValueError("PHASE4BS_STAGE_COVERAGE_OR_ORDER_INVALID")
    totals = {cause: 0 for cause in CAUSES}
    normalized: list[dict[str, Any]] = []
    expected_fields = {"stage", "started_at", "ended_at", "causes_ms", "evidence_hash"}
    for row in rows:
        if set(row) != expected_fields:
            raise ValueError("PHASE4BS_STAGE_FIELDS_INVALID")
        started, ended = _time(row["started_at"]), _time(row["ended_at"])
        if ended < started:
            raise ValueError("PHASE4BS_TEMPORAL_ORDER_INVALID")
        duration_ms = int((ended - started).total_seconds() * 1000)
        if duration_ms > MAX_DURATION_MS:
            raise ValueError("PHASE4BS_DURATION_BOUND_EXCEEDED")
        causes = row["causes_ms"]
        if not isinstance(causes, dict) or tuple(causes) != CAUSES:
            raise ValueError("PHASE4BS_CAUSE_COVERAGE_OR_ORDER_INVALID")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in causes.values()
        ):
            raise ValueError("PHASE4BS_CAUSE_VALUE_INVALID")
        if sum(causes.values()) != duration_ms:
            raise ValueError("PHASE4BS_AMBIGUOUS_OR_NONCONSERVING_ATTRIBUTION")
        if not isinstance(row["evidence_hash"], str) or len(row["evidence_hash"]) != 64:
            raise ValueError("PHASE4BS_EVIDENCE_HASH_INVALID")
        for cause, value in causes.items():
            totals[cause] += value
        normalized.append({**row, "duration_ms": duration_ms})
    grand_total = sum(totals.values())
    if grand_total <= 0:
        raise ValueError("PHASE4BS_ZERO_TOTAL_LATENCY")
    cause_rows = [
        {
            "cause": cause,
            "duration_ms": totals[cause],
            "share_basis_points": totals[cause] * 10_000 // grand_total,
        }
        for cause in CAUSES
    ]
    ordered_causes = sorted(
        cause_rows, key=lambda row: (-row["duration_ms"], CAUSES.index(row["cause"]))
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BS",
        "baseline_hash": payload["baseline_hash"],
        "budget_hash": payload["budget_hash"],
        "stages": normalized,
        "cause_totals": cause_rows,
        "total_attributed_ms": grand_total,
        "fully_attributed": True,
        "configuration_applied": False,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    ranking: dict[str, Any] = {
        "schema": CAUSE_SCHEMA,
        "phase": "4BS",
        "attribution_hash": report["artifact_hash"],
        "ordered_causes": ordered_causes,
        "primary_cause": ordered_causes[0]["cause"],
        "primary_cause_duration_ms": ordered_causes[0]["duration_ms"],
        "optimization_candidates_only": True,
        "execution_authorized": False,
    }
    ranking["artifact_hash"] = _hash(ranking)
    return report, ranking


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attribution-input", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--ranking-output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.attribution_input.read_text(encoding="utf-8"))
    report, ranking = build(payload)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.report_output, args.ranking_output, report, ranking)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
