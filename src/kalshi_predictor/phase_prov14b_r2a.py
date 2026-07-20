"""PROV-14B-R2A offline guarded certification evidence bundle."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_MODELS = ("crypto_v2", "weather_v2")


def build_certification_bundle(
    *,
    backup: Mapping[str, Any],
    rollback: Mapping[str, Any],
    safety: Mapping[str, Any],
    cycle: Mapping[str, Any],
    attribution: Mapping[str, Any],
    rollback_root: Path,
    as_of: datetime,
    synthetic_preview: bool = False,
    freshness_minutes: int = 15,
) -> dict[str, Any]:
    """Certify supplied evidence without opening a database or cloud connection."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if freshness_minutes < 1:
        raise ValueError("freshness_minutes must be positive")

    backup_gates = _backup_gates(backup, as_of)
    rollback_rows, rollback_gates = _rollback_gates(rollback, rollback_root)
    safety_gates = _safety_gates(safety, as_of, freshness_minutes)
    cycle_gates = _cycle_gates(cycle)
    attribution_gates = _attribution_gates(attribution, cycle)
    gates = {
        **{f"backup.{key}": value for key, value in backup_gates.items()},
        **{f"rollback.{key}": value for key, value in rollback_gates.items()},
        **{f"safety.{key}": value for key, value in safety_gates.items()},
        **{f"cycle.{key}": value for key, value in cycle_gates.items()},
        **{f"attribution.{key}": value for key, value in attribution_gates.items()},
    }
    passed = all(gates.values())
    report: dict[str, Any] = {
        "phase": "PROV-14B-R2A",
        "mode": (
            "LOCAL_SYNTHETIC_CERTIFICATION_BUNDLE_PREVIEW"
            if synthetic_preview
            else "LOCAL_OFFLINE_RUNTIME_EVIDENCE_CERTIFICATION"
        ),
        "status": "PASSED" if passed else "FAILED",
        "as_of": as_of.astimezone(UTC).isoformat(),
        "gates": gates,
        "failed_gates": sorted(key for key, value in gates.items() if not value),
        "rollback_files": rollback_rows,
        "observed": {
            "backup_path": backup.get("path"),
            "backup_sha256": backup.get("sha256"),
            "after_event_id": cycle.get("after_event_id"),
            "cycle_model_summaries": cycle.get("summaries"),
            "attribution_model_counts": attribution.get("summary", {}).get("model_counts"),
            "attribution_events": len(attribution.get("rows", []))
            if isinstance(attribution.get("rows"), list)
            else None,
        },
        "summary": {
            "bundle_passed": passed,
            "runtime_certified": passed and not synthetic_preview,
            "deployment_or_execution_authorized": False,
        },
        "guardrails": {
            "cloud_access": False,
            "database_opened": False,
            "database_writes": 0,
            "service_changes": 0,
            "threshold_changes": 0,
            "execution_enabled": False,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical(report).encode()).hexdigest()
    return report


def write_bundle(report: Mapping[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(output)
    return output


def _backup_gates(backup: Mapping[str, Any], as_of: datetime) -> dict[str, bool]:
    finished = _timestamp(backup.get("finished_at"))
    sha = str(backup.get("sha256") or "")
    path = str(backup.get("path") or "")
    age = (as_of.astimezone(UTC) - finished).total_seconds() if finished else -1
    return {
        "metadata_complete": all(
            backup.get(key) not in (None, "")
            for key in (
                "path",
                "size_bytes",
                "quick_check",
                "sha256",
                "integrity_check",
                "finished_at",
            )
        ),
        "path_is_absolute": Path(path).is_absolute(),
        "size_positive": _positive_int(backup.get("size_bytes")) is not None,
        "quick_check_ok": backup.get("quick_check") == "ok",
        "sha256_valid": len(sha) == 64 and all(char in "0123456789abcdef" for char in sha.lower()),
        "integrity_check_ok": backup.get("integrity_check") == "ok",
        "execution_disabled": backup.get("execution_enabled") is False,
        "timestamp_valid_and_not_future": finished is not None and age >= 0,
    }


def _rollback_gates(
    rollback: Mapping[str, Any], rollback_root: Path
) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    files = rollback.get("files")
    if not isinstance(files, list):
        files = []
    rows: list[dict[str, Any]] = []
    safe = True
    hashes_match = True
    unique: set[str] = set()
    for entry in files:
        if not isinstance(entry, Mapping):
            safe = False
            continue
        relative = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "")
        candidate = (rollback_root / relative).resolve()
        within_root = _is_relative_to(candidate, rollback_root.resolve())
        actual = _sha256(candidate) if within_root and candidate.is_file() else None
        matched = actual == expected and actual is not None
        safe = safe and within_root and bool(relative)
        hashes_match = hashes_match and matched
        if relative in unique:
            safe = False
        unique.add(relative)
        rows.append({
            "path": relative,
            "expected_sha256": expected,
            "actual_sha256": actual,
            "matched": matched,
            "within_rollback_root": within_root,
        })
    return rows, {
        "manifest_complete": bool(files),
        "paths_safe_and_unique": bool(files) and safe,
        "files_present_and_hashes_match": bool(files) and hashes_match,
    }


def _safety_gates(
    safety: Mapping[str, Any], as_of: datetime, freshness_minutes: int
) -> dict[str, bool]:
    captured = _timestamp(safety.get("captured_at"))
    age = (as_of.astimezone(UTC) - captured).total_seconds() if captured else -1
    services = safety.get("services") if isinstance(safety.get("services"), Mapping) else {}
    inactive = all(
        services.get(name) == "inactive"
        for name in ("bounded_service", "bounded_timer", "legacy_watcher", "other_writer")
    )
    return {
        "evidence_fresh": captured is not None and 0 <= age <= freshness_minutes * 60,
        "writer_clear": safety.get("safe_to_start_write") is True,
        "locks_clear": safety.get("locks_clear") is True,
        "execution_disabled": safety.get("execution_enabled") is False,
        "writer_services_inactive": inactive,
        "legacy_watcher_disabled": services.get("legacy_watcher_enabled") is False,
    }


def _cycle_gates(cycle: Mapping[str, Any]) -> dict[str, bool]:
    summaries = cycle.get("summaries") if isinstance(cycle.get("summaries"), Mapping) else {}
    tickers = cycle.get("tickers") if isinstance(cycle.get("tickers"), Mapping) else {}
    model_nonzero = all(
        isinstance(summaries.get(model), Mapping)
        and _positive_int(summaries[model].get("forecasts_inserted")) is not None
        for model in REQUIRED_MODELS
    )
    model_scanned = all(
        isinstance(summaries.get(model), Mapping)
        and _positive_int(summaries[model].get("snapshots_scanned")) is not None
        for model in REQUIRED_MODELS
    )
    ticker_sets = all(
        isinstance(tickers.get(model), list) and bool(tickers[model])
        for model in REQUIRED_MODELS
    )
    return {
        "after_event_id_valid": _nonnegative_int(cycle.get("after_event_id")) is not None,
        "both_models_scanned": model_scanned,
        "both_models_inserted_forecasts": model_nonzero,
        "both_models_have_pinned_tickers": ticker_sets,
        "weather_features_nonzero": (
            _positive_int(cycle.get("weather_features_inserted")) is not None
        ),
    }


def _attribution_gates(
    attribution: Mapping[str, Any], cycle: Mapping[str, Any]
) -> dict[str, bool]:
    summary = attribution.get("summary") if isinstance(attribution.get("summary"), Mapping) else {}
    rows = attribution.get("rows") if isinstance(attribution.get("rows"), list) else []
    model_counts = (
        summary.get("model_counts")
        if isinstance(summary.get("model_counts"), Mapping)
        else {}
    )
    exact_rows = bool(rows) and all(
        isinstance(row, Mapping)
        and row.get("passed") is True
        and not row.get("failures")
        and _positive_int(row.get("forecast_id")) is not None
        and isinstance(row.get("source_observation_ref"), Mapping)
        and _positive_int(row["source_observation_ref"].get("id")) is not None
        and bool(row["source_observation_ref"].get("table"))
        and _positive_int(row.get("market_snapshot_id")) is not None
        and bool(row.get("feature_source_table"))
        and _positive_int(row.get("feature_source_id")) is not None
        for row in rows
    )
    return {
        "phase_exact": attribution.get("phase") == "PROV-14",
        "boundary_matches_cycle": attribution.get("boundary", {}).get("after_event_id")
        == cycle.get("after_event_id"),
        "certification_passed": summary.get("certification_passed") is True,
        "no_failed_or_truncated_events": summary.get("events_failed") == 0
        and summary.get("result_truncated") is False,
        "both_models_nonzero": all(
            _positive_int(model_counts.get(model)) is not None
            for model in REQUIRED_MODELS
        ),
        "all_exact_references_present": exact_rows,
        "execution_disabled": attribution.get("guardrails", {}).get("execution_enabled") is False,
        "thresholds_unchanged": (
            attribution.get("guardrails", {}).get("thresholds_changed") is False
        ),
    }


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _positive_int(value: Any) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
