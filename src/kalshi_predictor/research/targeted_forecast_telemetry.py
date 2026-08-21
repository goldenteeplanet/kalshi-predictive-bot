"""Persistent event-level telemetry for targeted crypto forecasts."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def update_history(
    history: dict[str, Any],
    collector: dict[str, Any],
    alignment: dict[str, Any],
) -> dict[str, Any]:
    existing = {
        str(row["observation_id"]): row
        for row in history.get("observations", [])
        if isinstance(row, dict) and row.get("observation_id")
    }
    refresh = collector.get("targeted_forecast_refresh", {})
    targeted_rows = [
        row for row in refresh.get("rows", []) if isinstance(row, dict)
    ] if isinstance(refresh, dict) else []
    targeted_events = {str(row.get("event_ticker")) for row in targeted_rows}
    cycle = str(collector.get("generated_at") or _digest(targeted_rows))

    audits_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in alignment.get("alignment_audit_rows", []):
        if isinstance(row, dict):
            audits_by_event.setdefault(str(row.get("event_ticker")), []).append(row)
    latest_audits = {event: rows[0] for event, rows in audits_by_event.items()}

    for row in targeted_rows:
        event = str(row.get("event_ticker"))
        audit = _matching_targeted_audit(row, audits_by_event.get(event, []))
        immediate_capture = row.get("immediate_capture", {})
        observation = {
            "observation_id": _digest(["targeted", cycle, event]),
            "cycle_timestamp": cycle,
            "cohort": "targeted",
            "event_ticker": event,
            "family": str(row.get("family") or audit.get("family") or "UNKNOWN"),
            "forecast_succeeded": row.get("status") == "FORECASTED",
            "targeted_forecast_timestamp": row.get("forecast_timestamp"),
            "buckets_probed": int(row.get("buckets_probed", 0)),
            "midpoint_probe_failures": int(row.get("midpoint_probe_failures", 0)),
            "one_sided_bound_uses": int(row.get("one_sided_bound_uses", 0)),
            "aligned": bool(audit.get("aligned")) if audit else None,
            "forecast_capture_lag_seconds": audit.get("forecast_capture_lag_seconds"),
            "coverage_id": audit.get("coverage_id"),
            "immediate_capture_attempted": bool(immediate_capture.get("attempted")),
            "immediate_capture_within_budget": bool(
                immediate_capture.get("within_latency_budget")
            ),
            "capture_latency_budget_seconds": immediate_capture.get(
                "latency_budget_seconds"
            ),
            "reasons": row.get("reasons", []),
        }
        existing[observation["observation_id"]] = observation

    for event, audit in latest_audits.items():
        if event in targeted_events:
            continue
        coverage_id = audit.get("coverage_id")
        observation = {
            "observation_id": _digest(["historical", coverage_id, event]),
            "cycle_timestamp": audit.get("capture_timestamp"),
            "cohort": "historical",
            "event_ticker": event,
            "family": str(audit.get("family") or "UNKNOWN"),
            "forecast_succeeded": None,
            "buckets_probed": None,
            "midpoint_probe_failures": None,
            "one_sided_bound_uses": None,
            "aligned": bool(audit.get("aligned")),
            "forecast_capture_lag_seconds": audit.get("forecast_capture_lag_seconds"),
            "coverage_id": coverage_id,
            "reasons": audit.get("rejection_reasons", []),
        }
        existing.setdefault(observation["observation_id"], observation)

    observations = sorted(
        (_sanitize_observation(row) for row in existing.values()),
        key=lambda row: (str(row.get("cycle_timestamp")), row["observation_id"]),
    )
    return {
        "policy": "ROLLING_EVENT_LEVEL_TARGETED_VS_HISTORICAL_ALIGNMENT",
        "generated_at": datetime.now(UTC).isoformat(),
        "observations": observations,
        "summary": summarize(observations),
    }


def summarize(observations: list[dict[str, Any]]) -> dict[str, Any]:
    families = sorted({str(row.get("family") or "UNKNOWN") for row in observations})
    family_rows = [_summarize_family(family, observations) for family in families]
    overall = _summarize_family("ALL", observations)
    return {**overall, "families": family_rows}


def wilson_interval(successes: int, sample_size: int) -> dict[str, float | int | None]:
    if sample_size <= 0:
        return {
            "successes": successes,
            "sample_size": 0,
            "rate": None,
            "lower": None,
            "upper": None,
        }
    z = 1.959963984540054
    rate = successes / sample_size
    denominator = 1.0 + z * z / sample_size
    center = (rate + z * z / (2.0 * sample_size)) / denominator
    margin = z * math.sqrt(
        rate * (1.0 - rate) / sample_size + z * z / (4.0 * sample_size**2)
    ) / denominator
    return {
        "successes": successes,
        "sample_size": sample_size,
        "rate": rate,
        "lower": max(0.0, center - margin),
        "upper": min(1.0, center + margin),
    }


def write_history(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _summarize_family(
    family: str, observations: list[dict[str, Any]]
) -> dict[str, Any]:
    rows = observations if family == "ALL" else [
        row for row in observations if str(row.get("family") or "UNKNOWN") == family
    ]
    targeted = [row for row in rows if row.get("cohort") == "targeted"]
    forecasted = sum(row.get("forecast_succeeded") is True for row in targeted)
    targeted_alignment = [row for row in targeted if row.get("aligned") is not None]
    historical_alignment = [
        row for row in rows if row.get("cohort") == "historical" and row.get("aligned") is not None
    ]
    targeted_ci = wilson_interval(
        sum(row.get("aligned") is True for row in targeted_alignment),
        len(targeted_alignment),
    )
    historical_ci = wilson_interval(
        sum(row.get("aligned") is True for row in historical_alignment),
        len(historical_alignment),
    )
    improvement = None
    improvement_ci = {"lower": None, "upper": None}
    if targeted_ci["rate"] is not None and historical_ci["rate"] is not None:
        improvement = float(targeted_ci["rate"]) - float(historical_ci["rate"])
        improvement_ci = {
            "lower": float(targeted_ci["lower"]) - float(historical_ci["upper"]),
            "upper": float(targeted_ci["upper"]) - float(historical_ci["lower"]),
        }
    latencies = [
        float(row["forecast_capture_lag_seconds"])
        for row in targeted
        if row.get("forecast_capture_lag_seconds") is not None
    ]
    return {
        "family": family,
        "targeted_observations": len(targeted),
        "forecast_success": wilson_interval(forecasted, len(targeted)),
        "midpoint_probe_failures": sum(
            int(row.get("midpoint_probe_failures") or 0) for row in targeted
        ),
        "one_sided_bound_uses": sum(
            int(row.get("one_sided_bound_uses") or 0) for row in targeted
        ),
        "immediate_captures_attempted": sum(
            bool(row.get("immediate_capture_attempted")) for row in targeted
        ),
        "immediate_captures_within_budget": sum(
            bool(row.get("immediate_capture_within_budget")) for row in targeted
        ),
        "latency_sample_size": len(latencies),
        "mean_forecast_capture_lag_seconds": sum(latencies) / len(latencies) if latencies else None,
        "maximum_forecast_capture_lag_seconds": max(latencies, default=None),
        "targeted_alignment": targeted_ci,
        "historical_alignment": historical_ci,
        "aligned_yield_improvement": improvement,
        "aligned_yield_improvement_interval": improvement_ci,
    }


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _matching_targeted_audit(
    targeted: dict[str, Any], audits: list[dict[str, Any]]
) -> dict[str, Any]:
    forecast_timestamp = _timestamp(targeted.get("forecast_timestamp"))
    if targeted.get("status") != "FORECASTED" or forecast_timestamp is None:
        return {}
    ticker = str(targeted.get("ticker") or "")
    immediate_capture = targeted.get("immediate_capture", {})
    if immediate_capture:
        if not immediate_capture.get("within_latency_budget"):
            return {}
        expected_coverage_id = immediate_capture.get("coverage_id")
    else:
        expected_coverage_id = None
    for audit in audits:
        audit_timestamp = _timestamp(audit.get("forecast_timestamp"))
        if audit_timestamp is None:
            continue
        if ticker and str(audit.get("forecast_ticker") or "") != ticker:
            continue
        if expected_coverage_id is not None and audit.get("coverage_id") != expected_coverage_id:
            continue
        if abs((audit_timestamp - forecast_timestamp).total_seconds()) <= 0.001:
            return audit
    return {}


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except ValueError:
        return None


def _sanitize_observation(row: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(row)
    if sanitized.get("cohort") == "targeted" and not sanitized.get(
        "forecast_succeeded"
    ):
        sanitized["aligned"] = None
        sanitized["forecast_capture_lag_seconds"] = None
        sanitized["coverage_id"] = None
    return sanitized
