"""Unified health state for the active local fixed-rate scheduler."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def update_health(
    previous: dict[str, Any],
    *,
    action: str,
    cycle_id: str | None = None,
    cycle_started_at: str | None = None,
    stage: str | None = None,
    stage_started_at: str | None = None,
    exit_code: int | None = None,
    timeout_seconds: int | None = None,
    coinbase_report: dict[str, Any] | None = None,
    gh2_report: dict[str, Any] | None = None,
    cadence_seconds: int = 900,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    if action == "start":
        prior_cycles = int(previous.get("consecutive_healthy_cycles") or 0)
        return {
            "version": "fixed_rate_health_v1",
            "generated_at": now.isoformat(),
            "cycle_id": cycle_id,
            "cycle_started_at": cycle_started_at,
            "scheduler": {
                "status": "RUNNING",
                "current_stage": "cycle_start",
                "last_successful_completion": previous.get("scheduler", {}).get(
                    "last_successful_completion"
                ),
                "next_run_at": None,
            },
            "stages": {},
            "sources": {
                "websocket": {
                    "status": "NOT_APPLICABLE",
                    "applicability": "NOT_USED_BY_FIXED_RATE_REST_REFRESH",
                    "detail": (
                        "The active scheduler refreshes Kalshi through bounded REST "
                        "snapshots."
                    ),
                },
                "coinbase": {"status": "PENDING", "prices_imported": 0, "errors": []},
                "noaa": {"status": "PENDING", "features": 0, "forecasts": 0},
            },
            "timeout_reasons": [],
            "consecutive_healthy_cycles": prior_cycles,
            "required_healthy_cycles": 24,
            "overall_status": "RUNNING",
        }
    payload = json.loads(json.dumps(previous)) if previous else {}
    payload["generated_at"] = now.isoformat()
    if action == "stage":
        if not stage or exit_code is None:
            raise ValueError("stage action requires stage and exit_code")
        status = "COMPLETE" if exit_code == 0 else "TIMEOUT" if exit_code == 124 else "FAILED"
        timeout_reason = (
            f"{stage.upper()}_TIMEOUT_AFTER_{timeout_seconds}_SECONDS"
            if status == "TIMEOUT"
            else None
        )
        payload.setdefault("stages", {})[stage] = {
            "started_at": stage_started_at,
            "completed_at": now.isoformat(),
            "status": status,
            "exit_code": exit_code,
            "timeout_seconds": timeout_seconds,
            "timeout_reason": timeout_reason,
        }
        scheduler = payload.setdefault("scheduler", {})
        scheduler["status"] = "RUNNING"
        scheduler["current_stage"] = stage
        if timeout_reason:
            payload.setdefault("timeout_reasons", []).append(timeout_reason)
        if stage == "coinbase_stage":
            payload.setdefault("sources", {})["coinbase"] = _coinbase_state(
                coinbase_report or {}, stage_status=status
            )
        if stage == "gh2_decision_refresh":
            payload.setdefault("sources", {})["noaa"] = _noaa_state(
                gh2_report or {},
                stage_status=status,
                timeout_reason=timeout_reason,
                cycle_started_at=payload.get("cycle_started_at"),
            )
        return payload
    if action == "finish":
        stages = payload.get("stages", {})
        failures = [
            name for name, row in stages.items() if row.get("status") != "COMPLETE"
        ]
        coinbase_ok = payload.get("sources", {}).get("coinbase", {}).get("status") == "HEALTHY"
        healthy = not failures and coinbase_ok
        scheduler = payload.setdefault("scheduler", {})
        scheduler["status"] = "COMPLETE" if healthy else "COMPLETE_WITH_ATTENTION"
        scheduler["current_stage"] = "cycle_complete"
        scheduler["completed_at"] = now.isoformat()
        scheduler["next_run_at"] = (now + timedelta(seconds=cadence_seconds)).isoformat()
        if healthy:
            scheduler["last_successful_completion"] = now.isoformat()
            payload["consecutive_healthy_cycles"] = int(
                payload.get("consecutive_healthy_cycles") or 0
            ) + 1
        else:
            payload["consecutive_healthy_cycles"] = 0
        payload["overall_status"] = "HEALTHY" if healthy else "DEGRADED"
        return payload
    raise ValueError(f"unsupported action: {action}")


def write_health(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _coinbase_state(report: dict[str, Any], *, stage_status: str) -> dict[str, Any]:
    jobs = [row for row in report.get("jobs", []) if isinstance(row, dict)]
    errors = [str(error) for error in report.get("errors", [])]
    for job in jobs:
        errors.extend(str(error) for error in job.get("errors", []))
    prices = sum(int(row.get("quote_count") or 0) for row in jobs)
    healthy = stage_status == "COMPLETE" and prices > 0 and not errors
    return {
        "status": "HEALTHY" if healthy else "NEEDS_ATTENTION",
        "prices_imported": prices,
        "symbols": sorted(str(row.get("symbol")) for row in jobs if row.get("symbol")),
        "errors": errors,
        "report_generated_at": report.get("generated_at"),
    }


def _noaa_state(
    report: dict[str, Any],
    *,
    stage_status: str,
    timeout_reason: str | None,
    cycle_started_at: str | None,
) -> dict[str, Any]:
    decision = report.get("decision_refresh", {})
    features = sum(
        int(row.get("features_inserted") or 0)
        for row in decision.get("weather_features", [])
        if isinstance(row, dict)
    )
    forecasts = int(decision.get("weather_forecasts", {}).get("forecasts_inserted") or 0)
    report_generated_at = _parse_datetime(report.get("generated_at"))
    cycle_started = _parse_datetime(cycle_started_at)
    report_is_current = bool(
        report_generated_at
        and cycle_started
        and report_generated_at >= cycle_started
    )
    if stage_status == "TIMEOUT":
        status = "UNAVAILABLE_DUE_TO_STAGE_TIMEOUT"
        reason = timeout_reason
    elif stage_status != "COMPLETE":
        status = "NEEDS_ATTENTION"
        reason = f"GH2_DECISION_REFRESH_{stage_status}"
    elif not report_is_current:
        status = "STALE_OR_NOT_PUBLISHED"
        reason = "GH2_REPORT_NOT_UPDATED_IN_CURRENT_CYCLE"
    else:
        status = "HEALTHY" if features > 0 and forecasts > 0 else "NO_CURRENT_OUTPUT"
        reason = None if status == "HEALTHY" else "NOAA_PRODUCED_NO_CURRENT_OUTPUT"
    return {
        "status": status,
        "features": features,
        "forecasts": forecasts,
        "reason": reason,
        "report_generated_at": report.get("generated_at"),
    }


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
