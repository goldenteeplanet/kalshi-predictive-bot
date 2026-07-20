"""PROV-16 offline provenance retention, performance, and parity certification."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_MODELS = ("crypto_v2", "weather_v2")


def certify_provenance_export(
    *,
    events_path: Path,
    dashboard_path: Path,
    as_of: datetime,
    retention_days: int = 30,
    query_latency_limit_ms: float = 50.0,
) -> dict[str, Any]:
    """Certify bounded exports without opening a database or runtime service."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if retention_days < 1 or query_latency_limit_ms <= 0:
        raise ValueError("retention and latency limits must be positive")
    events_payload = _object(events_path)
    dashboard = _object(dashboard_path)
    events = events_payload.get("events")
    if not isinstance(events, list):
        raise ValueError("events export must contain an events array")

    normalized = [_event(row) for row in events]
    ordered = sorted(normalized, key=lambda row: (row["created_at"], row["event_id"]))
    unique_ids = len({row["event_id"] for row in ordered}) == len(ordered)
    chronological = normalized == ordered
    chain_valid = _chain_valid(ordered)
    ages = [(as_of.astimezone(UTC) - row["created_at"]).total_seconds() / 86400 for row in ordered]
    retention_valid = all(0 <= age <= retention_days for age in ages)

    started = time.perf_counter_ns()
    by_model = Counter(row["model_name"] for row in ordered)
    by_ticker = Counter(row["ticker"] for row in ordered)
    complete = sum(_references_complete(row) for row in ordered)
    query_ms = (time.perf_counter_ns() - started) / 1_000_000

    expected = {
        "event_count": len(ordered),
        "complete_reference_count": complete,
        "model_counts": dict(sorted(by_model.items())),
    }
    observed = {
        "event_count": dashboard.get("event_count"),
        "complete_reference_count": dashboard.get("complete_reference_count"),
        "model_counts": dashboard.get("model_counts"),
    }
    parity = expected == observed
    gates = {
        "bounded_export": len(ordered) <= 10_000,
        "chronological": chronological,
        "chain_valid": chain_valid,
        "complete_reference_coverage": complete == len(ordered) and bool(ordered),
        "dashboard_parity": parity,
        "event_export_nonempty": bool(ordered),
        "no_future_events": all(age >= 0 for age in ages),
        "query_latency": query_ms <= query_latency_limit_ms,
        "required_model_coverage": all(by_model[model] > 0 for model in REQUIRED_MODELS),
        "retention_valid": retention_valid,
        "unique_event_ids": unique_ids,
    }
    report = {
        "phase": "PROV-16",
        "mode": "OFFLINE_READ_ONLY_EXPORT_CERTIFICATION",
        "status": "PASSED" if all(gates.values()) else "FAILED",
        "as_of": as_of.astimezone(UTC).isoformat(),
        "source": {
            "events_path": str(events_path),
            "events_sha256": _sha256(events_path),
            "dashboard_path": str(dashboard_path),
            "dashboard_sha256": _sha256(dashboard_path),
        },
        "retention": {
            "retention_days": retention_days,
            "oldest_age_days": round(max(ages, default=0.0), 6),
            "newest_age_days": round(min(ages, default=0.0), 6),
        },
        "coverage": {
            "required_models": list(REQUIRED_MODELS),
            "model_counts": dict(sorted(by_model.items())),
            "complete_reference_count": complete,
        },
        "performance": {
            "query_latency_ms": round(query_ms, 6),
            "query_latency_limit_ms": query_latency_limit_ms,
            "ticker_cardinality": len(by_ticker),
        },
        "parity": {"expected": expected, "observed": observed},
        "gates": gates,
        "guardrails": {
            "cloud_access": False,
            "database_opened": False,
            "database_writes": 0,
            "execution_enabled": False,
            "threshold_changes": 0,
        },
    }
    report["report_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov16_retention_performance_dashboard_parity.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def _event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("each event must be an object")
    required = {"event_id", "ticker", "model_name", "created_at", "digest", "previous_digest"}
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"event missing fields: {','.join(missing)}")
    result = dict(value)
    result["created_at"] = _datetime(value["created_at"])
    return result


def _chain_valid(events: list[dict[str, Any]]) -> bool:
    previous = "GENESIS"
    for event in events:
        if event["previous_digest"] != previous or not event["digest"]:
            return False
        previous = str(event["digest"])
    return True


def _references_complete(event: dict[str, Any]) -> bool:
    return all(
        event.get(key) not in (None, "", "MISSING")
        for key in (
            "observation_id",
            "market_snapshot_id",
            "feature_set_id",
            "forecast_id",
        )
    )


def _datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("event created_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
