"""Build a deterministic read-only end-to-end time-to-trade latency baseline."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bp.latency-observation-set.v1"
REPORT_SCHEMA = "phase4bp.time-to-trade-baseline.v1"
BOTTLENECK_SCHEMA = "phase4bp.latency-bottleneck-report.v1"
STAGES = (
    "MARKET_OBSERVATION",
    "SNAPSHOT_READY",
    "FORECAST_READY",
    "RANKING_READY",
    "POSITION_SIZING_READY",
    "ADVANCED_RISK_READY",
    "PAPER_ELIGIBILITY_READY",
)
MAX_STAGE_DURATION_MS = 3_600_000
MAX_EVIDENCE_AGE_SECONDS = 86_400


def _hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("PHASE4BP_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4BP_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("PHASE4BP_TIMESTAMP_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def _percentile(values: list[int], percentile: int) -> int:
    ordered = sorted(values)
    rank = max(1, (percentile * len(ordered) + 99) // 100)
    return ordered[rank - 1]


def build(payload: dict[str, Any], *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BP_EVALUATION_TIMEZONE_MISSING")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BP_INPUT_SCHEMA_OR_HASH_INVALID")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("PHASE4BP_OBSERVATIONS_MISSING")
    traces: dict[str, list[dict[str, Any]]] = {}
    identities: set[tuple[str, str]] = set()
    for row in observations:
        if not isinstance(row, dict) or set(row) != {
            "trace_id",
            "stage",
            "started_at",
            "ended_at",
            "evidence_hash",
        }:
            raise ValueError("PHASE4BP_OBSERVATION_FIELDS_INVALID")
        trace_id, stage = row["trace_id"], row["stage"]
        if not isinstance(trace_id, str) or not trace_id or stage not in STAGES:
            raise ValueError("PHASE4BP_TRACE_OR_STAGE_INVALID")
        identity = (trace_id, stage)
        if identity in identities:
            raise ValueError("PHASE4BP_DUPLICATE_OBSERVATION")
        identities.add(identity)
        if not isinstance(row["evidence_hash"], str) or len(row["evidence_hash"]) != 64:
            raise ValueError("PHASE4BP_EVIDENCE_HASH_INVALID")
        traces.setdefault(trace_id, []).append(row)
    stage_durations: dict[str, list[int]] = {stage: [] for stage in STAGES}
    end_to_end: list[int] = []
    latest_end: datetime | None = None
    normalized: list[dict[str, Any]] = []
    for trace_id in sorted(traces):
        rows = traces[trace_id]
        if [row["stage"] for row in rows] != list(STAGES):
            raise ValueError("PHASE4BP_STAGE_COVERAGE_OR_ORDER_INVALID")
        previous_end: datetime | None = None
        trace_start: datetime | None = None
        for row in rows:
            started, ended = _time(row["started_at"]), _time(row["ended_at"])
            if ended < started or (previous_end is not None and started < previous_end):
                raise ValueError("PHASE4BP_TEMPORAL_ORDER_INVALID")
            duration_ms = int((ended - started).total_seconds() * 1000)
            if duration_ms > MAX_STAGE_DURATION_MS:
                raise ValueError("PHASE4BP_DURATION_BOUND_EXCEEDED")
            trace_start = trace_start or started
            previous_end = ended
            latest_end = ended if latest_end is None or ended > latest_end else latest_end
            stage_durations[row["stage"]].append(duration_ms)
            normalized.append({**row, "duration_ms": duration_ms})
        assert trace_start is not None and previous_end is not None
        end_to_end.append(int((previous_end - trace_start).total_seconds() * 1000))
    assert latest_end is not None
    evidence_age = int((now.astimezone(UTC) - latest_end).total_seconds())
    if evidence_age < 0:
        raise ValueError("PHASE4BP_FUTURE_EVIDENCE")
    if evidence_age > MAX_EVIDENCE_AGE_SECONDS:
        raise ValueError("PHASE4BP_STALE_EVIDENCE")
    summaries = [
        {
            "stage": stage,
            "sample_count": len(stage_durations[stage]),
            "p50_ms": _percentile(stage_durations[stage], 50),
            "p95_ms": _percentile(stage_durations[stage], 95),
            "p99_ms": _percentile(stage_durations[stage], 99),
            "max_ms": max(stage_durations[stage]),
        }
        for stage in STAGES
    ]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4BP",
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "input_hash": payload["artifact_hash"],
        "trace_count": len(traces),
        "observations": normalized,
        "stage_summaries": summaries,
        "end_to_end": {
            "p50_ms": _percentile(end_to_end, 50),
            "p95_ms": _percentile(end_to_end, 95),
            "p99_ms": _percentile(end_to_end, 99),
            "max_ms": max(end_to_end),
        },
        "evidence_age_seconds": evidence_age,
        "production_records_created": 0,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    worst = max(summaries, key=lambda row: (row["p95_ms"], -STAGES.index(row["stage"])))
    bottleneck: dict[str, Any] = {
        "schema": BOTTLENECK_SCHEMA,
        "phase": "4BP",
        "baseline_hash": report["artifact_hash"],
        "primary_bottleneck_stage": worst["stage"],
        "primary_bottleneck_p95_ms": worst["p95_ms"],
        "ordered_stage_p95_ms": [
            {"stage": row["stage"], "p95_ms": row["p95_ms"]}
            for row in sorted(
                summaries, key=lambda row: (-row["p95_ms"], STAGES.index(row["stage"]))
            )
        ],
        "measurement_only": True,
        "execution_authorized": False,
    }
    bottleneck["artifact_hash"] = _hash(bottleneck)
    return report, bottleneck


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--baseline-output", type=Path, required=True)
    parser.add_argument("--bottleneck-output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.observations.read_text(encoding="utf-8"))
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    report, bottleneck = build(payload, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.baseline_output, args.bottleneck_output, report, bottleneck)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
