from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PRIORITY = {
    "VERIFIED_LINK_MISSING": 10,
    "SOURCE_MISSING": 20,
    "SNAPSHOT_MISSING": 30,
    "SNAPSHOT_STALE": 40,
    "EV_NOT_POSITIVE": 50,
    "NO_CURRENT_ROWS": 60,
}


def build_readiness2_preview(
    blockers_path: Path,
    summary_path: Path,
    *,
    as_of: datetime,
    stale_after_hours: int = 24,
) -> dict[str, Any]:
    rows = list(csv.DictReader(blockers_path.read_text(encoding="utf-8").splitlines()))
    if not rows:
        raise ValueError("READINESS-1 blocker evidence is empty")
    summary_text = summary_path.read_text(encoding="utf-8")
    generated_at = _generated_at(summary_text)
    age_hours = (as_of.astimezone(UTC) - generated_at).total_seconds() / 3600
    if age_hours < 0:
        raise ValueError("READINESS-1 generated-at evidence is in the future")
    source_stale = age_hours > stale_after_hours
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "affected_rows": 0,
            "categories": set(),
            "models": set(),
            "next_actions": set(),
        }
    )
    category_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        blocker = str(row.get("blocker") or "UNKNOWN")
        if blocker not in PRIORITY:
            raise ValueError(f"READINESS-1 blocker is not exact/recognized: {blocker}")
        count = int(row.get("blocker_count") or 0)
        if count < 0:
            raise ValueError(f"READINESS-1 blocker count is negative: {blocker}")
        grouped[blocker]["affected_rows"] += count
        grouped[blocker]["categories"].add(str(row.get("category") or "unknown"))
        grouped[blocker]["models"].add(str(row.get("model_name") or "unknown"))
        if row.get("next_action"):
            grouped[blocker]["next_actions"].add(str(row["next_action"]))
        category = str(row.get("category") or "unknown")
        metrics = {
            "model_name": row.get("model_name"),
            "current_rows": int(row.get("current_rows") or 0),
            "paper_ready_rows": int(row.get("paper_ready_rows") or 0),
            "positive_ev_rows": int(row.get("positive_ev_rows") or 0),
            "first_blocker": row.get("first_blocker"),
        }
        numeric_keys = ("current_rows", "paper_ready_rows", "positive_ev_rows")
        if any(metrics[key] < 0 for key in numeric_keys):
            raise ValueError(f"READINESS-1 category metrics are negative: {category}")
        if metrics["paper_ready_rows"] > metrics["current_rows"]:
            raise ValueError(f"READINESS-1 paper-ready rows exceed current rows: {category}")
        if metrics["positive_ev_rows"] > metrics["current_rows"]:
            raise ValueError(f"READINESS-1 positive-EV rows exceed current rows: {category}")
        if category in category_rows:
            existing = category_rows[category]
            if any(existing[key] != value for key, value in metrics.items()):
                raise ValueError(f"READINESS-1 category metrics conflict: {category}")
        else:
            category_rows[category] = {"category": category, **metrics, "blockers": []}
        category_rows[category]["blockers"].append(
            {"blocker": blocker, "affected_rows": count}
        )
    attribution = [
        {
            "blocker": blocker,
            "priority": PRIORITY.get(blocker, 999),
            "affected_rows": values["affected_rows"],
            "categories": sorted(values["categories"]),
            "models": sorted(values["models"]),
            "next_actions": sorted(values["next_actions"]),
            "threshold_change_required": False,
        }
        for blocker, values in grouped.items()
    ]
    attribution.sort(key=lambda item: (item["priority"], -item["affected_rows"], item["blocker"]))
    total_current = sum(item["current_rows"] for item in category_rows.values())
    total_ready = sum(item["paper_ready_rows"] for item in category_rows.values())
    report: dict[str, Any] = {
        "phase": "READINESS-2",
        "status": "STALE_READINESS_1_EVIDENCE" if source_stale else "PASSED_READ_ONLY_PREVIEW",
        "mode": "LOCAL_READ_ONLY_FAILED_GATE_ATTRIBUTION",
        "source": {
            "blockers_path": str(blockers_path),
            "summary_path": str(summary_path),
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "age_hours": round(age_hours, 3),
            "stale_after_hours": stale_after_hours,
            "stale": source_stale,
        },
        "observed": {
            "current_rows": total_current,
            "paper_ready_rows": total_ready,
            "paper_ready_open": total_ready > 0,
            "categories": [category_rows[key] for key in sorted(category_rows)],
        },
        "blocker_attribution": attribution,
        "decision": (
            "RERUN_READINESS_1_BEFORE_REMEDIATION"
            if source_stale
            else "REMEDIATE_IN_PRIORITY_ORDER_WITH_UNCHANGED_GATES"
        ),
        "guardrails": {
            "database_writes": 0,
            "threshold_changes": 0,
            "paper_orders_created": 0,
            "live_orders_created": 0,
            "execution_enabled": False,
            "stale_evidence_can_authorize_activation": False,
        },
        "next_phase": "READINESS-1 — Fresh Consolidated Paper-Readiness Gate Recheck",
    }
    report["report_sha256"] = hashlib.sha256(
        (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    return report


def write_readiness2_preview(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _generated_at(text: str) -> datetime:
    match = re.search(r"^- Generated at: `([^`]+)`", text, re.MULTILINE)
    if not match:
        raise ValueError("READINESS-1 generated-at evidence is missing")
    parsed = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("READINESS-1 generated-at evidence lacks timezone")
    return parsed.astimezone(UTC)
