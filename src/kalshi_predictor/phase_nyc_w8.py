"""NYC-W8 scheduled live-shadow cadence and drift certification."""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


MIN_DISTINCT_WINDOWS = 3
MAX_ALIGNMENT_SECONDS = 900


def write_nyc_w8_report(*, reports_dir: Path, output_dir: Path) -> Path:
    """Census W7 shadow artifacts without changing runtime or database state."""
    windows: dict[str, dict[str, Any]] = {}
    blocker_counts: Counter[str] = Counter()

    for path in sorted(
        reports_dir.glob("phase_nyc_w7_live_*/nyc_w7_shadow_observation_runtime_report.json")
    ):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            blocker_counts["SHADOW_REPORT_UNREADABLE"] += 1
            continue
        if payload.get("feature_flag") != "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false":
            blocker_counts["FEATURE_FLAG_NOT_DISABLED"] += 1
        if payload.get("execution_enabled") is not False:
            blocker_counts["EXECUTION_ENABLED"] += 1
        if payload.get("database_writes") != 0:
            blocker_counts["DATABASE_WRITE_DETECTED"] += 1
        for row in payload.get("rows", []):
            provenance = row.get("provenance") or {}
            target = str(
                provenance.get("target_utc_time")
                or row.get("target_utc_time")
                or ""
            )
            ticker = str(row.get("ticker") or "")
            window_id = target or _ticker_window(ticker)
            if not window_id:
                blocker_counts["WINDOW_ID_MISSING"] += 1
                continue
            window = windows.setdefault(
                window_id,
                {
                    "window_id": window_id,
                    "target_utc_time": target or None,
                    "rows": [],
                    "source_reports": [],
                },
            )
            window["source_reports"].append(str(path))
            window["rows"].append(row)

    certified_windows = 0
    total_rows = 0
    shadow_changes: list[Decimal] = []
    ordered_windows: list[dict[str, Any]] = []
    for window_id in sorted(windows):
        window = windows[window_id]
        row_blockers: Counter[str] = Counter()
        source_rows = window.pop("rows")
        for row in source_rows:
            total_rows += 1
            for blocker in _row_blockers(row):
                row_blockers[blocker] += 1
                blocker_counts[blocker] += 1
            change = to_decimal(row.get("shadow_change"))
            if change is not None:
                shadow_changes.append(change)
        window["source_reports"] = sorted(set(window["source_reports"]))
        window["row_count"] = len(source_rows)
        window["blocker_counts"] = dict(sorted(row_blockers.items()))
        window["passed"] = not row_blockers
        if window["passed"]:
            certified_windows += 1
        ordered_windows.append(window)

    gates = {
        "minimum_distinct_live_windows": len(ordered_windows) >= MIN_DISTINCT_WINDOWS,
        "all_windows_drift_free": bool(ordered_windows) and certified_windows == len(ordered_windows),
        "rollback_continuously_verified": blocker_counts["ROLLBACK_NOT_EXACT"] == 0,
        "feature_flag_remains_disabled": blocker_counts["FEATURE_FLAG_NOT_DISABLED"] == 0,
        "execution_remains_disabled": blocker_counts["EXECUTION_ENABLED"] == 0,
        "database_remains_read_only": blocker_counts["DATABASE_WRITE_DETECTED"] == 0,
    }
    census_passed = all(gates.values())
    report = {
        "phase": "NYC-W8",
        "generated_at": utc_now().isoformat(),
        "mode": "SCHEDULED_LIVE_SHADOW_DRIFT_CERTIFICATION",
        "database_writes": 0,
        "execution_enabled": False,
        "feature_flag": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false",
        "thresholds_changed": False,
        "requirements": {
            "minimum_distinct_live_windows": MIN_DISTINCT_WINDOWS,
            "maximum_alignment_seconds": MAX_ALIGNMENT_SECONDS,
        },
        "windows": ordered_windows,
        "summary": {
            "distinct_live_windows": len(ordered_windows),
            "certified_live_windows": certified_windows,
            "shadow_rows": total_rows,
            "minimum_shadow_change": str(min(shadow_changes)) if shadow_changes else None,
            "maximum_shadow_change": str(max(shadow_changes)) if shadow_changes else None,
            "mean_absolute_shadow_change": (
                str(sum(map(abs, shadow_changes), Decimal("0")) / len(shadow_changes))
                if shadow_changes else None
            ),
            "drift_blocker_counts": dict(sorted(blocker_counts.items())),
            "gates": gates,
            "live_shadow_census_passed": census_passed,
            "status": "CERTIFIED" if census_passed else "COLLECTING_LIVE_WINDOWS",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "nyc_w8_live_shadow_drift_certification.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _row_blockers(row: dict[str, Any]) -> list[str]:
    provenance = row.get("provenance") or {}
    blockers: list[str] = []
    if not row.get("passed"):
        blockers.append("SHADOW_EVALUATION_FAILED")
    if row.get("runtime_applied"):
        blockers.append("ROLLBACK_NOT_EXACT")
    if row.get("runtime_probability") != row.get("baseline_probability"):
        blockers.append("ROLLBACK_NOT_EXACT")
    if provenance.get("evidence_source") != "NOAA_KNYC":
        blockers.append("EVIDENCE_SOURCE_DRIFT")
    if provenance.get("evidence_role") != "NON_SETTLEMENT_POINT_OBSERVATION":
        blockers.append("EVIDENCE_ROLE_DRIFT")
    if provenance.get("settlement_source") != "THE_WEATHER_COMPANY":
        blockers.append("SETTLEMENT_SOURCE_DRIFT")
    if provenance.get("station_id") != "KNYC":
        blockers.append("STATION_DRIFT")
    if str(provenance.get("target_utc_time") or "") != str(row.get("target_utc_time") or ""):
        blockers.append("TARGET_TIME_DRIFT")
    offset = to_decimal(provenance.get("offset_seconds"))
    if offset is None or abs(offset) > MAX_ALIGNMENT_SECONDS:
        blockers.append("ALIGNMENT_DRIFT")
    baseline = to_decimal(row.get("baseline_probability", row.get("runtime_probability")))
    shadow = to_decimal(row.get("shadow_probability"))
    if baseline is None or shadow is None or not (Decimal("0.01") <= shadow <= Decimal("0.99")):
        blockers.append("PROBABILITY_INVALID")
    return sorted(set(blockers))


def _ticker_window(ticker: str) -> str:
    parts = ticker.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else ""
