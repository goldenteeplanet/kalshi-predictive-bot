from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import parse_datetime

DEFAULT_THRESHOLDS = {
    "minimum_coverage_ratio": 1.0,
    "maximum_observation_age_seconds": 900.0,
    "maximum_snapshot_age_seconds": 120.0,
}


def build_attribution_regression_report(
    events: Iterable[Mapping[str, Any]],
    *,
    expected_model_versions: Mapping[str, Iterable[str]],
    generated_at: datetime,
    thresholds: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Evaluate synthetic attribution envelopes without database or runtime access."""
    limits = _thresholds(thresholds)
    expected = {
        str(model): tuple(sorted({str(version) for version in versions}))
        for model, versions in sorted(expected_model_versions.items())
    }
    ordered = sorted(
        (dict(event) for event in events),
        key=lambda row: (str(row.get("event_at") or ""), str(row.get("event_key") or "")),
    )
    rows = [_evaluate_event(event, limits) for event in ordered]
    observed = Counter((row["model_name"], row["model_version"]) for row in rows)
    missing_revisions = [
        {"model_name": model, "model_version": version}
        for model, versions in expected.items()
        for version in versions
        if observed[(model, version)] == 0
    ]
    required_revision_count = sum(len(versions) for versions in expected.values())
    covered_revision_count = required_revision_count - len(missing_revisions)
    coverage_ratio = (
        covered_revision_count / required_revision_count if required_revision_count else 1.0
    )
    failure_counts = Counter(failure for row in rows for failure in row["failures"])
    passed = (
        bool(rows)
        and not failure_counts
        and not missing_revisions
        and coverage_ratio >= limits["minimum_coverage_ratio"]
    )
    return {
        "phase": "PROV-15",
        "generated_at": generated_at.isoformat(),
        "mode": "SYNTHETIC_ATTRIBUTION_DRIFT_REGRESSION",
        "database_access": False,
        "execution_enabled": False,
        "thresholds": limits,
        "summary": {
            "passed": passed,
            "events_examined": len(rows),
            "events_passed": sum(row["passed"] for row in rows),
            "events_failed": sum(not row["passed"] for row in rows),
            "required_revision_count": required_revision_count,
            "covered_revision_count": covered_revision_count,
            "coverage_ratio": round(coverage_ratio, 6),
            "missing_revisions": missing_revisions,
            "failure_counts": dict(sorted(failure_counts.items())),
        },
        "rows": rows,
    }


def write_attribution_regression_report(
    events: Iterable[Mapping[str, Any]],
    *,
    expected_model_versions: Mapping[str, Iterable[str]],
    generated_at: datetime,
    output_path: Path,
    thresholds: Mapping[str, float] | None = None,
) -> Path:
    report = build_attribution_regression_report(
        events,
        expected_model_versions=expected_model_versions,
        generated_at=generated_at,
        thresholds=thresholds,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return output_path


def _evaluate_event(event: Mapping[str, Any], limits: Mapping[str, float]) -> dict[str, Any]:
    failures: list[str] = []
    event_at = parse_datetime(event.get("event_at"))
    observation = event.get("source_observation_ref")
    snapshot = event.get("market_snapshot_ref")
    observation_at = _reference_time(observation, ("observed_at", "forecast_generated_at"))
    snapshot_at = _reference_time(snapshot, ("captured_at",))
    observation_age = _age_seconds(event_at, observation_at)
    snapshot_age = _age_seconds(event_at, snapshot_at)
    if not _valid_reference(observation):
        failures.append("OBSERVATION_REFERENCE_MISSING")
    elif observation_at is None or event_at is None:
        failures.append("OBSERVATION_TIMESTAMP_INVALID")
    elif observation_age is not None and (
        observation_age < 0
        or observation_age > limits["maximum_observation_age_seconds"]
    ):
        failures.append("OBSERVATION_STALE")
    if not _valid_reference(snapshot):
        failures.append("SNAPSHOT_REFERENCE_MISSING")
    elif snapshot_at is None or event_at is None:
        failures.append("SNAPSHOT_TIMESTAMP_INVALID")
    elif snapshot_age is not None and (
        snapshot_age < 0 or snapshot_age > limits["maximum_snapshot_age_seconds"]
    ):
        failures.append("SNAPSHOT_STALE")
    return {
        "event_key": str(event.get("event_key") or ""),
        "ticker": str(event.get("ticker") or ""),
        "model_name": str(event.get("model_name") or ""),
        "model_version": str(event.get("model_version") or ""),
        "event_at": event_at.isoformat() if event_at else None,
        "observation_age_seconds": observation_age,
        "snapshot_age_seconds": snapshot_age,
        "passed": not failures,
        "failures": failures,
    }


def _thresholds(overrides: Mapping[str, float] | None) -> dict[str, float]:
    result = dict(DEFAULT_THRESHOLDS)
    if overrides:
        unknown = set(overrides) - set(result)
        if unknown:
            raise ValueError(f"unknown thresholds: {sorted(unknown)}")
        result.update({key: float(value) for key, value in overrides.items()})
    if not 0 <= result["minimum_coverage_ratio"] <= 1:
        raise ValueError("minimum_coverage_ratio must be between 0 and 1")
    if result["maximum_observation_age_seconds"] < 0:
        raise ValueError("maximum_observation_age_seconds must be non-negative")
    if result["maximum_snapshot_age_seconds"] < 0:
        raise ValueError("maximum_snapshot_age_seconds must be non-negative")
    return result


def _valid_reference(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and bool(value.get("table"))
        and _positive_int(value.get("id"))
    )


def _reference_time(value: Any, keys: tuple[str, ...]) -> datetime | None:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        parsed = parse_datetime(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _age_seconds(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds(), 6)


def _positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False
