"""Bounded read-only view of prospective research, separate from paper admission."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .current_research_store import read_current_records
from .store import aware


def empty_current_research() -> dict[str, Any]:
    return {
        "evidence_status": "UNVERIFIED", "journal_records": None,
        "scan_count": None, "assessment_count": None, "prospective_shadow_count": None,
        "shadow_state_counts": None, "evaluated_shadow_count": None,
        "latest_scan_at": None, "latest_scan_age_seconds": None,
        "latest_scan_freshness": "UNVERIFIED", "latest_scan_funnel": None,
        "latest_scan_first_blocker_counts": None, "latest_scan_scope": None,
        "funnel_scope": "LATEST_SCAN_ONLY_NOT_LIFETIME_TOTAL",
        "runtime_verified": False, "paper_eligible": False,
        "full_net_point_estimate_status": "UNKNOWN",
    }


def current_research_snapshot(db: sqlite3.Connection, *, now: datetime) -> dict[str, Any]:
    """Validate the complete bounded journal; never silently truncate its history."""
    records = read_current_records(db, max_records=10000)
    if now.utcoffset() is None:
        raise ValueError("CURRENT_RESEARCH_DASHBOARD_AWARE_CLOCK_REQUIRED")
    at = aware(now.isoformat())
    scans = []
    shadows: dict[str, dict[str, Any]] = {}
    states: dict[str, str] = {}
    assessed = 0
    for envelope in records:
        recorded = aware(envelope["recorded_at"])
        if recorded > at:
            raise ValueError("CURRENT_RESEARCH_DASHBOARD_FUTURE_CLOCK")
        kind, record = envelope["record_kind"], envelope["record"]
        if kind == "SCAN":
            clock = aware(record["assessed_at"])
            if clock > recorded:
                raise ValueError("CURRENT_RESEARCH_SCAN_CLOCK_INVALID")
            for field in ("funnel", "first_blocker_counts"):
                counts = record[field]
                if not isinstance(counts, dict) or any(
                    not isinstance(k, str) or type(v) is not int or v < 0
                    for k, v in counts.items()
                ):
                    raise ValueError("CURRENT_RESEARCH_SCAN_COUNTS_INVALID")
            scans.append((clock, envelope["journal_id"], record))
        elif kind == "ASSESSMENT":
            assessed += 1
        elif kind == "PROSPECTIVE_SHADOW":
            decision_id = record["decision_id"]
            if decision_id in shadows:
                raise ValueError("CURRENT_RESEARCH_DUPLICATE_SHADOW")
            shadows[decision_id] = record
            states[decision_id] = "OPEN"
    evaluated: set[str] = set()
    for envelope in records:
        kind, record = envelope["record_kind"], envelope["record"]
        if kind not in {"SHADOW_OBSERVATION", "EVALUATION"}:
            continue
        decision_id = (
            record["evaluation"]["decision_id"] if kind == "EVALUATION" else record["decision_id"]
        )
        if decision_id not in shadows:
            raise ValueError("CURRENT_RESEARCH_SHADOW_LINK_MISSING")
        if kind == "EVALUATION":
            if (decision_id in evaluated or record["evaluation"]["state"] != "EVALUATED"
                    or record["decision"] != {
                        k: v for k, v in shadows[decision_id].items() if k != "execution_authority"
                    }):
                raise ValueError("CURRENT_RESEARCH_EVALUATION_LINK_INVALID")
            evaluated.add(decision_id)
            states[decision_id] = "EVALUATED"
        else:
            state = record["state"]
            if state not in {"CLOSED", "AWAITING_FINAL", "FINAL"}:
                raise ValueError("CURRENT_RESEARCH_OBSERVATION_STATE_INVALID")
            if decision_id not in evaluated:
                states[decision_id] = state
    result = empty_current_research()
    result.update(
        evidence_status="VERIFIED_JOURNAL_NOT_RUNTIME_CERTIFICATION",
        journal_records=len(records), scan_count=len(scans), assessment_count=assessed,
        prospective_shadow_count=len(shadows), evaluated_shadow_count=len(evaluated),
        shadow_state_counts={
            state: sum(value == state for value in states.values())
            for state in ("OPEN", "CLOSED", "AWAITING_FINAL", "FINAL", "EVALUATED")
        },
    )
    if scans:
        clock, _, latest = max(scans, key=lambda item: (item[0], item[1]))
        age = (at-clock).total_seconds()
        result.update(
            latest_scan_at=clock.isoformat(), latest_scan_age_seconds=age,
            latest_scan_freshness="RECENT_RECORDED_SCAN" if age <= 300 else "STALE",
            latest_scan_funnel=latest["funnel"],
            latest_scan_first_blocker_counts=latest["first_blocker_counts"],
            latest_scan_scope=latest.get("scope"),
        )
    return result
