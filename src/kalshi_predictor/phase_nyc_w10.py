"""NYC-W10 guarded, read-only live-shadow stability review."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

MAX_MEAN_ABSOLUTE_SHADOW_CHANGE = Decimal("0.10")


def write_nyc_w10_review(
    *, w8_report: Path, w9_report: Path, output_dir: Path,
    operations_evidence: dict[str, Any] | None = None,
) -> Path:
    w8 = json.loads(w8_report.read_text(encoding="utf-8"))
    w9 = json.loads(w9_report.read_text(encoding="utf-8"))
    summary = w8.get("summary", {})
    w8_gates = summary.get("gates", {})
    mean_change = to_decimal(summary.get("mean_absolute_shadow_change"))
    operations = operations_evidence or {}
    gates = {
        "three_new_live_windows_certified": (
            int(summary.get("certified_live_windows") or 0) >= 3
            and bool(summary.get("live_shadow_census_passed"))
        ),
        "provenance_and_alignment_drift_free": (
            bool(w8_gates.get("all_windows_drift_free"))
            and not summary.get("drift_blocker_counts")
        ),
        "rollback_continuously_verified": bool(
            w8_gates.get("rollback_continuously_verified")
        ),
        "probability_effect_within_review_limit": (
            mean_change is not None and mean_change <= MAX_MEAN_ABSOLUTE_SHADOW_CHANGE
        ),
        "collector_completed_without_state_reset": (
            w9.get("status") == "COMPLETE" and w9.get("state_reset") is not True
        ),
        "scheduler_operationally_stable": (
            operations.get("w8_timer_active") is True
            and operations.get("w9_timer_active") is True
            and int(operations.get("failed_runs") or 0) == 0
        ),
        "feature_flag_still_disabled": (
            w8.get("feature_flag") == "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false"
            and w9.get("feature_flag_enabled") is False
        ),
        "execution_still_disabled": (
            w8.get("execution_enabled") is False and w9.get("execution_enabled") is False
        ),
    }
    ready = all(gates.values())
    report = {
        "phase": "NYC-W10", "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_GUARDED_ACTIVATION_DECISION_PREVIEW",
        "source_reports": {"nyc_w8": str(w8_report), "nyc_w9": str(w9_report)},
        "database_writes": 0, "thresholds_changed": False,
        "feature_flag_changed": False, "execution_enabled": False,
        "automatic_activation_permitted": False,
        "review_limits": {
            "maximum_mean_absolute_shadow_change": str(MAX_MEAN_ABSOLUTE_SHADOW_CHANGE),
        },
        "observed": {
            "certified_live_windows": int(summary.get("certified_live_windows") or 0),
            "mean_absolute_shadow_change": (
                str(mean_change) if mean_change is not None else None
            ),
            "drift_blocker_counts": summary.get("drift_blocker_counts", {}),
            "collector_status": w9.get("status"),
            "operations": operations,
        },
        "gates": gates,
        "summary": {
            "review_ready": ready,
            "decision": (
                "ELIGIBLE_FOR_MANUAL_ACTIVATION_REVIEW"
                if ready else "BLOCKED_PENDING_LIVE_SHADOW_EVIDENCE"
            ),
            "automatic_action_taken": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "nyc_w10_live_shadow_stability_review.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path
