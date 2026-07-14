from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import parse_datetime, utc_now

R5_PRIMARY_EV_NOT_POSITIVE = "EV_NOT_POSITIVE"
R5_ALIGNMENT_REASON = "R5_POST_REFRESH_CLEARED_STALE_AND_RANKING_GAPS"

R5_STATUS_PATH = Path("phase3bc_r5") / "phase3bc_r5_status.json"
R5_WATCH_PATH = Path("phase3bc_r5") / "phase3bc_r5_crypto_freshness_watch.json"


def load_r5_truth_alignment(
    reports_dir: Path,
    *,
    max_status_age_minutes: int = 120,
    now: Any | None = None,
) -> dict[str, Any]:
    """Load the latest R5 post-refresh blocker when it proves stale gaps are clear."""

    resolved_now = now or utc_now()
    skipped: list[dict[str, Any]] = []
    for relative_path, summary_key in (
        (R5_STATUS_PATH, "latest_summary"),
        (R5_WATCH_PATH, "summary"),
    ):
        path = reports_dir / relative_path
        payload = _read_json(path)
        if not payload:
            skipped.append({"path": str(path), "reason": "MISSING_OR_INVALID"})
            continue

        generated_at = parse_datetime(payload.get("generated_at"))
        age_minutes = _age_minutes(generated_at, resolved_now)
        if age_minutes is not None and age_minutes > max_status_age_minutes:
            skipped.append(
                {
                    "path": str(path),
                    "reason": "R5_STATUS_TOO_OLD",
                    "age_minutes": round(age_minutes, 3),
                }
            )
            continue

        summary = payload.get(summary_key)
        if not isinstance(summary, dict):
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        evidence = _alignment_evidence(
            summary,
            path=path,
            generated_at=payload.get("generated_at"),
            latest_report_generated_at=payload.get("latest_report_generated_at"),
            age_minutes=age_minutes,
        )
        if evidence["applies"]:
            return {**evidence, "skipped_sources": skipped}
        skipped.append(
            {
                "path": str(path),
                "reason": evidence["reason"],
                "primary_gap_after_refresh": evidence.get("primary_gap_after_refresh"),
            }
        )

    return {
        "applies": False,
        "reason": "NO_FRESH_R5_EV_NOT_POSITIVE_ALIGNMENT",
        "skipped_sources": skipped,
    }


def apply_r5_truth_to_blocker_summary(
    summary: dict[str, Any],
    *,
    blocker_key: str,
    reports_dir: Path,
    raw_key: str | None = None,
) -> dict[str, Any]:
    alignment = load_r5_truth_alignment(reports_dir)
    summary["r5_truth_alignment"] = alignment
    summary["r5_alignment_applied"] = bool(alignment.get("applies"))
    if not alignment.get("applies"):
        return alignment

    raw_blocker_key = raw_key or f"raw_{blocker_key}"
    summary[raw_blocker_key] = summary.get(blocker_key)
    summary[blocker_key] = alignment["primary_gap_after_refresh"]
    summary["r5_alignment_reason"] = alignment["reason"]
    summary["r5_alignment_source_path"] = alignment["source_path"]
    summary["r5_alignment_latest_report_generated_at"] = alignment.get(
        "latest_report_generated_at"
    )
    return alignment


def _alignment_evidence(
    summary: dict[str, Any],
    *,
    path: Path,
    generated_at: Any,
    latest_report_generated_at: Any,
    age_minutes: float | None,
) -> dict[str, Any]:
    primary_gap = summary.get("primary_gap_after_refresh")
    snapshot_stale = _int_value(summary.get("snapshot_stale_rows"))
    forecast_stale = _int_value(summary.get("forecast_stale_rows"))
    ranking_gap = _ranking_gap_after_repair(summary)
    applies = (
        primary_gap == R5_PRIMARY_EV_NOT_POSITIVE
        and snapshot_stale == 0
        and forecast_stale == 0
        and ranking_gap == 0
    )
    return {
        "applies": applies,
        "reason": R5_ALIGNMENT_REASON if applies else "R5_DOES_NOT_CLEAR_STALE_GAPS",
        "source_path": str(path),
        "status_generated_at": generated_at,
        "latest_report_generated_at": latest_report_generated_at or generated_at,
        "status_age_minutes": round(age_minutes, 3) if age_minutes is not None else None,
        "watch_state": summary.get("watch_state"),
        "primary_gap_after_refresh": primary_gap,
        "snapshot_stale_rows": snapshot_stale,
        "forecast_stale_rows": forecast_stale,
        "ranking_coverage_gap_after_repair": ranking_gap,
        "true_ranking_gap_after_repair": _int_value(
            summary.get("true_ranking_gap_after_repair")
        ),
        "ranking_missing_rows": _int_value(summary.get("ranking_missing_rows")),
        "ranking_stale_rows": _int_value(summary.get("ranking_stale_rows")),
        "ranking_before_forecast_rows": _int_value(
            summary.get("ranking_before_forecast_rows")
        ),
        "positive_ev_rows": _int_value(summary.get("positive_ev_rows")),
        "clean_execution_rows": _int_value(summary.get("clean_execution_rows")),
        "paper_ready_candidates": _int_value(summary.get("paper_ready_candidates")),
    }


def _ranking_gap_after_repair(summary: dict[str, Any]) -> int | None:
    explicit_gap = _int_value(summary.get("ranking_coverage_gap_after_repair"))
    if explicit_gap is not None:
        return explicit_gap
    true_gap = _int_value(summary.get("true_ranking_gap_after_repair"))
    if true_gap is not None:
        return true_gap
    components = [
        _int_value(summary.get("ranking_missing_rows")),
        _int_value(summary.get("ranking_stale_rows")),
        _int_value(summary.get("ranking_before_forecast_rows")),
    ]
    if any(value is not None for value in components):
        return sum(value or 0 for value in components)
    return None


def _age_minutes(generated_at: Any, now: Any) -> float | None:
    if generated_at is None:
        return None
    return max(0.0, (now - generated_at).total_seconds() / 60)


def _int_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
