from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.provenance.regression import build_attribution_regression_report

ALLOWED_REPAIR_FIELDS = {
    "source_observation_ref", "market_snapshot_ref", "model_version"
}


def simulate_exact_attribution_repairs(
    events: Iterable[Mapping[str, Any]],
    *,
    repairs_by_event_key: Mapping[str, Mapping[str, Any]],
    expected_model_versions: Mapping[str, Iterable[str]],
    generated_at: datetime,
    thresholds: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    source = [copy.deepcopy(dict(event)) for event in events]
    repaired = copy.deepcopy(source)
    index = _unique_event_index(repaired)
    applied = []
    for event_key in sorted(repairs_by_event_key):
        if event_key not in index:
            raise ValueError(f"repair event key not found: {event_key}")
        repair = repairs_by_event_key[event_key]
        if not isinstance(repair, Mapping):
            raise ValueError(f"repair for {event_key} must be an object")
        unknown = set(repair) - ALLOWED_REPAIR_FIELDS
        if unknown:
            raise ValueError(f"unsupported repair fields for {event_key}: {sorted(unknown)}")
        _validate_exact_repair(event_key, repair)
        changed = []
        for field in sorted(repair):
            value = copy.deepcopy(repair[field])
            if index[event_key].get(field) != value:
                index[event_key][field] = value
                changed.append(field)
        applied.append({"event_key": event_key, "changed_fields": changed})
    before = build_attribution_regression_report(
        source, expected_model_versions=expected_model_versions,
        generated_at=generated_at, thresholds=thresholds,
    )
    after = build_attribution_regression_report(
        repaired, expected_model_versions=expected_model_versions,
        generated_at=generated_at, thresholds=thresholds,
    )
    return {
        "phase": "PROV-15E",
        "generated_at": generated_at.isoformat(),
        "mode": "OFFLINE_EXACT_REMEDIATION_BEFORE_AFTER_SIMULATION",
        "database_access": False,
        "runtime_configuration_changed": False,
        "execution_enabled": False,
        "repairs_applied": applied,
        "before": before,
        "after": after,
        "delta": {
            "events_passed": (
                after["summary"]["events_passed"] - before["summary"]["events_passed"]
            ),
            "events_failed": (
                after["summary"]["events_failed"] - before["summary"]["events_failed"]
            ),
            "coverage_ratio": round(
                after["summary"]["coverage_ratio"] - before["summary"]["coverage_ratio"], 6
            ),
            "failure_counts": _failure_delta(
                before["summary"]["failure_counts"], after["summary"]["failure_counts"]
            ),
            "mean_observation_age_seconds": _mean_age_delta(
                before["rows"], after["rows"], "observation_age_seconds"
            ),
            "mean_snapshot_age_seconds": _mean_age_delta(
                before["rows"], after["rows"], "snapshot_age_seconds"
            ),
        },
        "certification": {
            "before_passed": before["summary"]["passed"],
            "after_passed": after["summary"]["passed"],
            "exact_repairs_only": True,
            "source_rows_mutated": False,
        },
    }


def write_exact_remediation_simulation(
    events: Iterable[Mapping[str, Any]],
    *,
    repairs_by_event_key: Mapping[str, Mapping[str, Any]],
    expected_model_versions: Mapping[str, Iterable[str]],
    generated_at: datetime,
    output_path: Path,
    thresholds: Mapping[str, float] | None = None,
) -> Path:
    report = simulate_exact_attribution_repairs(
        events,
        repairs_by_event_key=repairs_by_event_key,
        expected_model_versions=expected_model_versions,
        generated_at=generated_at,
        thresholds=thresholds,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return output_path


def _unique_event_index(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for event in events:
        key = str(event.get("event_key") or "")
        if not key:
            raise ValueError("every event requires an event_key")
        if key in result:
            raise ValueError(f"duplicate event_key: {key}")
        result[key] = event
    return result


def _validate_exact_repair(event_key: str, repair: Mapping[str, Any]) -> None:
    for field in ("source_observation_ref", "market_snapshot_ref"):
        if field not in repair:
            continue
        value = repair[field]
        if not isinstance(value, Mapping) or not value.get("table") or not value.get("id"):
            raise ValueError(f"{field} for {event_key} requires exact table and id")
        timestamp_keys = (
            ("observed_at", "forecast_generated_at")
            if field == "source_observation_ref" else ("captured_at",)
        )
        if not any(value.get(key) for key in timestamp_keys):
            raise ValueError(f"{field} for {event_key} requires an exact timestamp")
    if "model_version" in repair and not str(repair["model_version"]).strip():
        raise ValueError(f"model_version for {event_key} must be non-empty")


def _failure_delta(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    return {
        key: int(after.get(key, 0)) - int(before.get(key, 0))
        for key in sorted(set(before) | set(after))
    }


def _mean_age_delta(
    before: list[Mapping[str, Any]], after: list[Mapping[str, Any]], field: str
) -> dict[str, float | None]:
    before_mean = _mean(row.get(field) for row in before)
    after_mean = _mean(row.get(field) for row in after)
    return {
        "before": before_mean,
        "after": after_mean,
        "change": (
            round(after_mean - before_mean, 6)
            if before_mean is not None and after_mean is not None else None
        ),
    }


def _mean(values: Iterable[Any]) -> float | None:
    numeric = [float(value) for value in values if value is not None]
    return round(sum(numeric) / len(numeric), 6) if numeric else None
