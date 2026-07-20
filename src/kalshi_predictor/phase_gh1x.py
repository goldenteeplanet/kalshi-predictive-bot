"""GH-1X read-only liquidity and executable-edge census."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any


def build_gh1x_census(source: dict[str, Any]) -> dict[str, Any]:
    if source.get("phase") != "GH-1V":
        raise ValueError("source must be a GH-1V report")
    summary = source.get("summary", {})
    windows = source.get("windows", [])
    rows = source.get("positive_edge_rows", [])
    if not isinstance(windows, list) or not isinstance(rows, list):
        raise ValueError("windows and positive_edge_rows must be arrays")

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("positive edge rows must be objects")
        key = (str(row.get("window_id")), str(row.get("ticker")))
        candidate = _normalize(row)
        current = unique.get(key)
        if (
            current is None
            or candidate["remaining_blocker_count"] < current["remaining_blocker_count"]
        ):
            unique[key] = candidate
    candidates = sorted(unique.values(), key=lambda row: (row["window_id"], row["ticker"]))
    blocker_counts = Counter(blocker for row in candidates for blocker in row["blockers"])
    naturally_advanced = [row for row in candidates if row["advance"] and not row["blockers"]]
    gates = {
        "execution_disabled": source.get("execution_enabled") is False
        and summary.get("execution_remains_disabled") is True,
        "multi_window_complete": summary.get("multi_window_complete") is True,
        "three_distinct_windows": len({str(row.get("window_id")) for row in windows}) >= 3,
        "thresholds_unchanged": source.get("thresholds_changed") is False,
        "window_sources_present": all(row.get("source_path") for row in windows),
    }
    reported_window_positive = sum(int(row.get("positive_edge", 0)) for row in windows)
    report: dict[str, Any] = {
        "phase": "GH-1X",
        "mode": "READ_ONLY_MULTI_WINDOW_GATE_CENSUS",
        "status": "PASSED" if all(gates.values()) else "FAILED",
        "source_generated_at": source.get("generated_at"),
        "windows": windows,
        "counts": {
            "reported_evaluations": int(summary.get("evaluated", 0)),
            "reported_positive_edge_evaluations": int(summary.get("positive_edge", 0)),
            "window_positive_edge_sum": reported_window_positive,
            "unique_positive_edge_candidates": len(candidates),
            "naturally_advanced_candidates": len(naturally_advanced),
            "duplicate_positive_edge_rows": len(rows) - len(candidates),
        },
        "blocker_counts_unique_candidates": dict(sorted(blocker_counts.items())),
        "unique_positive_edge_candidates": candidates,
        "advanced_candidates": naturally_advanced,
        "gates": gates,
        "decision": (
            "ADVANCE_NATURAL_PASSERS"
            if naturally_advanced
            else "CLOSE_CANDIDATE_SET_AND_RETURN_TO_BOUNDED_DISCOVERY"
            if all(gates.values())
            else "DO_NOT_ADVANCE_INVALID_CENSUS"
        ),
        "guardrails": {
            "cloud_access": False,
            "database_writes": 0,
            "execution_enabled": False,
            "threshold_changes": 0,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical(report).encode()).hexdigest()
    return report


def write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1x_liquidity_edge_risk_census.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(path)
    return path


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    blockers = sorted({str(value) for value in row.get("blockers", [])})
    return {
        "window_id": str(row.get("window_id")),
        "ticker": str(row.get("ticker")),
        "model_name": str(row.get("model_name")),
        "executable_edge": str(Decimal(str(row.get("executable_edge")))),
        "opportunity_score": str(Decimal(str(row.get("opportunity_score")))),
        "liquidity_score": str(Decimal(str(row.get("liquidity_score")))),
        "spread": None if row.get("spread") is None else str(Decimal(str(row["spread"]))),
        "time_to_close_minutes": str(Decimal(str(row.get("time_to_close_minutes")))),
        "blockers": blockers,
        "remaining_blocker_count": len(blockers),
        "advance": bool(row.get("advance")),
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
