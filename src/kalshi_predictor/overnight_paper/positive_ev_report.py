"""Bounded archived-artifact diagnostics using only the standard library."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .scan_diagnostics import frozen_economics, summarize_funnel

MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 1000


def read_positive_ev_report(artifact: Path, *, limit: int = 10) -> dict[str, Any]:
    if not 1 <= limit <= 50:
        raise ValueError("REPORT_LIMIT_1_TO_50")
    with artifact.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("ARTIFACT_EXCEEDS_4_MIB")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("QUALIFIED_SCAN_ROWS_REQUIRED")
    originals = payload["rows"]
    if len(originals) > MAX_ROWS or any(not isinstance(row, dict) for row in originals):
        raise ValueError("ARTIFACT_REQUIRES_AT_MOST_1000_OBJECT_ROWS")
    rows = []
    for original in originals:
        inputs = original.get("frozen_decision_inputs")
        has_inputs = isinstance(inputs, dict) and bool(inputs)
        row = frozen_economics(inputs if has_inputs else {})
        row.update(
            ticker=inputs.get("ticker") if has_inputs else original.get("ticker"),
            category=inputs.get("category") if has_inputs else original.get("category"),
            first_blocker="ARCHIVED_INPUTS_NOT_SEMANTICALLY_REVERIFIED"
            if has_inputs
            else "ORIGINAL_DECISION_INPUTS_MISSING",
        )
        # Never copy externally supplied PASS, preparation, book or readiness flags.
        rows.append(row)
    known = [row for row in rows if row["net_ev"] is not None]
    known.sort(key=lambda row: Decimal(row["net_ev"]), reverse=True)
    near_misses = [row for row in known if row["strictly_above_recorded_threshold"] is False]
    near_misses.sort(key=lambda row: Decimal(row["minimum_net_ev"]) - Decimal(row["net_ev"]))
    return {
        "status": "ARCHIVED_FROZEN_DIAGNOSTICS_ONLY",
        "authority": "NO_EXECUTION_AUTHORITY",
        "current_net_ev": None,
        "semantic_verification": "NOT_PERFORMED",
        "notice": (
            "Arithmetic recomputed from archived inputs; source authenticity and gates unverified."
        ),
        "reported_candidates": len(rows),
        "funnel": summarize_funnel(rows),
        "top_frozen_candidates": known[:limit],
        "near_misses_to_recorded_threshold": near_misses[:limit],
        "rows": rows,
    }


def render_positive_ev_report(report: dict[str, Any]) -> str:
    lines = [
        "Archived / frozen positive-EV diagnostics",
        "No execution authority. Current net EV: unknown.",
        report["notice"],
        f"Candidates: {report['reported_candidates']}",
    ]
    for label, key in (
        ("Top frozen candidates", "top_frozen_candidates"),
        ("Near misses to recorded threshold", "near_misses_to_recorded_threshold"),
    ):
        lines.append(label + ":")
        entries = report[key]
        lines.extend(
            f"  {row.get('ticker')}: frozen net EV={row['net_ev']}; "
            f"recorded threshold={row['minimum_net_ev']}"
            for row in entries
        )
        if not entries:
            lines.append("  None with sufficient archived inputs.")
    lines.append(
        "Category frozen economics: "
        + json.dumps(report["funnel"]["frozen_economics"]["by_category"])
    )
    lines.append("First blockers: " + json.dumps(report["funnel"]["first_blocker_counts"]))
    return "\n".join(lines)
