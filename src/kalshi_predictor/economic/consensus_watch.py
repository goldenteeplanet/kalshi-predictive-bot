from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import EconomicEvent
from kalshi_predictor.economic.actuals import (
    TRADING_ECONOMICS_ENV_NAMES,
    run_phase3bd_r4_verified_consensus_source,
)
from kalshi_predictor.utils.time import utc_now

KEY_RELEASE_EVENT_KEYS = ("cpi", "jobs", "gdp", "fed")
DEFAULT_PRE_RELEASE_MINUTES = 180
DEFAULT_POST_RELEASE_MINUTES = 360

R4Runner = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class Phase3BDR5Artifacts:
    json_path: Path
    markdown_path: Path
    history_path: Path
    payload: dict[str, Any]


def run_phase3bd_r5_consensus_feed_watch(
    session: Session,
    *,
    input_file: Path | None = None,
    trading_economics_api_key: str | None = None,
    country: str = "united states",
    days_back: int = 90,
    days_ahead: int = 14,
    min_importance: int = 2,
    pre_release_minutes: int = DEFAULT_PRE_RELEASE_MINUTES,
    post_release_minutes: int = DEFAULT_POST_RELEASE_MINUTES,
    force_refresh: bool = False,
    now: datetime | None = None,
    cycle_number: int = 1,
    total_cycles: int = 1,
    r4_runner: R4Runner | None = None,
    max_series: int = 24,
    markets_per_series: int = 50,
    snapshot_series_limit: int = 8,
    forecast_limit: int = 500,
    opportunity_limit: int = 75,
) -> dict[str, Any]:
    generated_at = now or utc_now()
    generated_at = _as_utc(generated_at)
    source_state = _source_state(
        input_file=input_file,
        trading_economics_api_key=trading_economics_api_key,
    )
    window_state = release_window_state(
        session,
        now=generated_at,
        pre_release_minutes=pre_release_minutes,
        post_release_minutes=post_release_minutes,
        days_back=days_back,
        days_ahead=days_ahead,
    )
    should_run_r4 = bool(
        source_state["source_configured"]
        and (force_refresh or window_state["in_release_window"])
    )
    r4_payload: dict[str, Any] | None = None
    if should_run_r4:
        runner = r4_runner or run_phase3bd_r4_verified_consensus_source
        r4_payload = runner(
            session,
            input_file=input_file,
            trading_economics_api_key=trading_economics_api_key,
            country=country,
            days_back=days_back,
            days_ahead=days_ahead,
            min_importance=min_importance,
            max_series=max_series,
            markets_per_series=markets_per_series,
            snapshot_series_limit=snapshot_series_limit,
            forecast_limit=forecast_limit,
            opportunity_limit=opportunity_limit,
        )

    summary = _summary(
        source_state=source_state,
        window_state=window_state,
        should_run_r4=should_run_r4,
        force_refresh=force_refresh,
        r4_payload=r4_payload,
        cycle_number=cycle_number,
        total_cycles=total_cycles,
    )
    payload = {
        "phase": "3BD-R5",
        "generated_at": generated_at.isoformat(),
        "mode": "PAPER_READ_ONLY_CONSENSUS_FEED_RELEASE_WINDOW_WATCH",
        "live_demo_execution": "blocked",
        "order_submission_cancel_replace": "blocked",
        "summary": summary,
        "source_state": source_state,
        "release_window": window_state,
        "r4": _r4_report_summary(r4_payload),
        "config": {
            "country": country,
            "days_back": days_back,
            "days_ahead": days_ahead,
            "min_importance": min_importance,
            "pre_release_minutes": pre_release_minutes,
            "post_release_minutes": post_release_minutes,
            "force_refresh": force_refresh,
            "max_series": max_series,
            "markets_per_series": markets_per_series,
            "snapshot_series_limit": snapshot_series_limit,
            "forecast_limit": forecast_limit,
            "opportunity_limit": opportunity_limit,
        },
        "recommended_next_action": _recommended_next_action(
            source_state=source_state,
            window_state=window_state,
            summary=summary,
        ),
    }
    return payload


def write_phase3bd_r5_consensus_feed_watch_report(
    *,
    session: Session,
    output_dir: Path,
    input_file: Path | None = None,
    trading_economics_api_key: str | None = None,
    country: str = "united states",
    days_back: int = 90,
    days_ahead: int = 14,
    min_importance: int = 2,
    pre_release_minutes: int = DEFAULT_PRE_RELEASE_MINUTES,
    post_release_minutes: int = DEFAULT_POST_RELEASE_MINUTES,
    force_refresh: bool = False,
    now: datetime | None = None,
    cycle_number: int = 1,
    total_cycles: int = 1,
    r4_runner: R4Runner | None = None,
    max_series: int = 24,
    markets_per_series: int = 50,
    snapshot_series_limit: int = 8,
    forecast_limit: int = 500,
    opportunity_limit: int = 75,
) -> Phase3BDR5Artifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = run_phase3bd_r5_consensus_feed_watch(
        session,
        input_file=input_file,
        trading_economics_api_key=trading_economics_api_key,
        country=country,
        days_back=days_back,
        days_ahead=days_ahead,
        min_importance=min_importance,
        pre_release_minutes=pre_release_minutes,
        post_release_minutes=post_release_minutes,
        force_refresh=force_refresh,
        now=now,
        cycle_number=cycle_number,
        total_cycles=total_cycles,
        r4_runner=r4_runner,
        max_series=max_series,
        markets_per_series=markets_per_series,
        snapshot_series_limit=snapshot_series_limit,
        forecast_limit=forecast_limit,
        opportunity_limit=opportunity_limit,
    )
    json_path = output_dir / "phase3bd_r5_consensus_feed_watch.json"
    markdown_path = output_dir / "phase3bd_r5_consensus_feed_watch.md"
    history_path = output_dir / "phase3bd_r5_consensus_feed_watch_history.jsonl"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_history_entry(payload), sort_keys=True) + "\n")
    return Phase3BDR5Artifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        history_path=history_path,
        payload=payload,
    )


def release_window_state(
    session: Session,
    *,
    now: datetime | None = None,
    pre_release_minutes: int = DEFAULT_PRE_RELEASE_MINUTES,
    post_release_minutes: int = DEFAULT_POST_RELEASE_MINUTES,
    days_back: int = 90,
    days_ahead: int = 14,
) -> dict[str, Any]:
    current = _as_utc(now or utc_now())
    start = current - timedelta(days=max(days_back, 0))
    end = current + timedelta(days=max(days_ahead, 0))
    statement = (
        select(EconomicEvent)
        .where(EconomicEvent.event_key.in_(KEY_RELEASE_EVENT_KEYS))
        .where(EconomicEvent.event_time >= start)
        .where(EconomicEvent.event_time <= end)
        .order_by(EconomicEvent.event_time, EconomicEvent.id)
    )
    events = list(session.scalars(statement))
    event_payloads = [_release_event_payload(event, now=current) for event in events]
    in_window_events = [
        row
        for row in event_payloads
        if -post_release_minutes <= row["minutes_until_release"] <= pre_release_minutes
    ]
    next_event = next(
        (row for row in event_payloads if row["minutes_until_release"] >= 0),
        None,
    )
    previous_events = [
        row for row in event_payloads if row["minutes_until_release"] < 0
    ]
    previous_event = previous_events[-1] if previous_events else None
    return {
        "checked_at": current.isoformat(),
        "tracked_event_keys": list(KEY_RELEASE_EVENT_KEYS),
        "events_scanned": len(event_payloads),
        "release_window_events": in_window_events,
        "in_release_window": bool(in_window_events),
        "pre_release_minutes": pre_release_minutes,
        "post_release_minutes": post_release_minutes,
        "next_release_event_key": (next_event or {}).get("event_key"),
        "next_release_title": (next_event or {}).get("title"),
        "next_release_time": (next_event or {}).get("event_time"),
        "minutes_until_next_release": (next_event or {}).get("minutes_until_release"),
        "last_release_event_key": (previous_event or {}).get("event_key"),
        "last_release_title": (previous_event or {}).get("title"),
        "last_release_time": (previous_event or {}).get("event_time"),
        "minutes_since_last_release": (
            abs(previous_event["minutes_until_release"]) if previous_event else None
        ),
        "events": event_payloads[:50],
    }


def _source_state(
    *,
    input_file: Path | None,
    trading_economics_api_key: str | None,
) -> dict[str, Any]:
    configured_env_names = [name for name in TRADING_ECONOMICS_ENV_NAMES if os.getenv(name)]
    api_configured = bool(trading_economics_api_key or configured_env_names)
    file_configured = input_file is not None
    if api_configured and file_configured:
        source_mode = "TRADING_ECONOMICS_API_AND_VERIFIED_INPUT_FILE"
    elif api_configured:
        source_mode = "TRADING_ECONOMICS_API"
    elif file_configured:
        source_mode = "VERIFIED_INPUT_FILE"
    else:
        source_mode = "NONE"
    return {
        "source_configured": api_configured or file_configured,
        "source_mode": source_mode,
        "trading_economics_api_configured": api_configured,
        "verified_input_file_configured": file_configured,
        "verified_input_file": str(input_file) if input_file is not None else None,
        "configured_env_names": configured_env_names,
        "credential_value_reported": False,
    }


def _summary(
    *,
    source_state: dict[str, Any],
    window_state: dict[str, Any],
    should_run_r4: bool,
    force_refresh: bool,
    r4_payload: dict[str, Any] | None,
    cycle_number: int,
    total_cycles: int,
) -> dict[str, Any]:
    r4_summary = (r4_payload or {}).get("summary", {})
    status = _status(
        source_state=source_state,
        window_state=window_state,
        should_run_r4=should_run_r4,
        r4_status=r4_summary.get("status"),
    )
    return {
        "status": status,
        "cycle_number": cycle_number,
        "total_cycles": total_cycles,
        "source_configured": bool(source_state["source_configured"]),
        "source_mode": source_state["source_mode"],
        "in_release_window": bool(window_state["in_release_window"]),
        "release_window_event_count": len(window_state["release_window_events"]),
        "next_release_event_key": window_state["next_release_event_key"],
        "next_release_time": window_state["next_release_time"],
        "minutes_until_next_release": window_state["minutes_until_next_release"],
        "last_release_event_key": window_state["last_release_event_key"],
        "last_release_time": window_state["last_release_time"],
        "minutes_since_last_release": window_state["minutes_since_last_release"],
        "force_refresh": force_refresh,
        "r4_ran": should_run_r4,
        "r4_status": r4_summary.get("status"),
        "sources_attempted": r4_summary.get("sources_attempted", 0),
        "sources_succeeded": r4_summary.get("sources_succeeded", 0),
        "consensus_value_observations": r4_summary.get(
            "consensus_value_observations",
            0,
        ),
        "actual_and_consensus_observations": r4_summary.get(
            "actual_and_consensus_observations",
            0,
        ),
        "features_inserted": r4_summary.get("features_inserted", 0),
        "forecasts_inserted": r4_summary.get("forecasts_inserted", 0),
        "rankings_inserted": r4_summary.get("rankings_inserted", 0),
        "opportunities_detected": r4_summary.get("opportunities_detected", 0),
        "paper_only_safety": "preserved",
        "live_demo_execution": "blocked",
        "order_submission_cancel_replace": "blocked",
    }


def _status(
    *,
    source_state: dict[str, Any],
    window_state: dict[str, Any],
    should_run_r4: bool,
    r4_status: str | None,
) -> str:
    if not source_state["source_configured"]:
        return "BLOCKED_BY_MISSING_CONSENSUS_SOURCE"
    if not window_state["events_scanned"]:
        return "WAITING_FOR_RELEASE_CALENDAR"
    if not should_run_r4:
        return "WAITING_FOR_RELEASE_WINDOW"
    if r4_status:
        return f"R4_{r4_status}"
    return "CONSENSUS_REFRESH_ATTEMPTED"


def _r4_report_summary(r4_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if r4_payload is None:
        return None
    return {
        "generated_at": r4_payload.get("generated_at"),
        "summary": r4_payload.get("summary", {}),
        "recommended_next_action": r4_payload.get("recommended_next_action"),
        "sources": r4_payload.get("sources", []),
        "opportunity_report": r4_payload.get("opportunity_report"),
    }


def _release_event_payload(event: EconomicEvent, *, now: datetime) -> dict[str, Any]:
    event_time = _as_utc(event.event_time)
    minutes_until = int((event_time - now).total_seconds() // 60)
    raw = decode_json(event.raw_json)
    if minutes_until > 0:
        timing = "PRE_RELEASE"
    elif minutes_until < 0:
        timing = "POST_RELEASE"
    else:
        timing = "AT_RELEASE"
    return {
        "event_key": event.event_key,
        "category": event.category,
        "title": event.title,
        "source": event.source,
        "source_url": raw.get("source_url"),
        "event_time": event_time.isoformat(),
        "minutes_until_release": minutes_until,
        "timing": timing,
        "has_actual": event.actual_value is not None,
        "has_consensus": event.forecast_value is not None,
        "has_previous": event.previous_value is not None,
    }


def _recommended_next_action(
    *,
    source_state: dict[str, Any],
    window_state: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    if not source_state["source_configured"]:
        return (
            "Configure TRADING_ECONOMICS_API_KEY or pass --input-file with verified "
            "consensus rows before enabling the release-window watch."
        )
    if not window_state["events_scanned"]:
        return (
            "Refresh Phase 3BD-R2 economic calendar so CPI/jobs/GDP/Fed release "
            "windows are available."
        )
    if not window_state["in_release_window"] and not summary["force_refresh"]:
        return (
            "Keep the watch scheduled; R4 will run inside the configured release "
            "window or when --force-refresh is supplied."
        )
    if summary["actual_and_consensus_observations"] > 0:
        return (
            "Review refreshed economic_v1 rankings and opportunities; execution "
            "remains paper/read-only."
        )
    if summary["consensus_value_observations"] > 0:
        return "Consensus rows are loaded; keep watching for actual release values."
    return "R4 ran without usable consensus rows; verify source mapping and date range."


def _history_entry(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload["summary"]
    return {
        "generated_at": payload["generated_at"],
        "status": summary["status"],
        "cycle_number": summary["cycle_number"],
        "source_mode": summary["source_mode"],
        "in_release_window": summary["in_release_window"],
        "r4_ran": summary["r4_ran"],
        "r4_status": summary["r4_status"],
        "consensus_value_observations": summary["consensus_value_observations"],
        "actual_and_consensus_observations": summary[
            "actual_and_consensus_observations"
        ],
        "features_inserted": summary["features_inserted"],
        "forecasts_inserted": summary["forecasts_inserted"],
        "rankings_inserted": summary["rankings_inserted"],
        "opportunities_detected": summary["opportunities_detected"],
    }


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    source_state = payload["source_state"]
    next_release = _event_label(
        summary["next_release_event_key"],
        summary["next_release_time"],
    )
    last_release = _event_label(
        summary["last_release_event_key"],
        summary["last_release_time"],
    )
    lines = [
        "# Phase 3BD-R5 Economic Consensus Feed Watch",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "Mode: PAPER / READ ONLY. Live/demo execution remains blocked.",
        "",
        "## Summary",
        "",
        f"- Status: {summary['status']}",
        f"- Cycle: {summary['cycle_number']} / {summary['total_cycles']}",
        f"- Source mode: {summary['source_mode']}",
        f"- In release window: {summary['in_release_window']}",
        f"- R4 ran: {summary['r4_ran']}",
        f"- R4 status: {summary['r4_status'] or 'n/a'}",
        f"- Consensus observations: {summary['consensus_value_observations']}",
        f"- Actual + consensus observations: {summary['actual_and_consensus_observations']}",
        f"- Features inserted: {summary['features_inserted']}",
        f"- Forecasts inserted: {summary['forecasts_inserted']}",
        f"- Rankings inserted: {summary['rankings_inserted']}",
        f"- Opportunities detected: {summary['opportunities_detected']}",
        "",
        "## Release Window",
        "",
        f"- Next release: {next_release}",
        f"- Minutes until next release: {summary['minutes_until_next_release']}",
        f"- Last release: {last_release}",
        f"- Minutes since last release: {summary['minutes_since_last_release']}",
        "",
        "| Key | Time | Minutes | Timing | Source | Title |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for row in payload["release_window"]["release_window_events"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["event_key"]),
                    str(row["event_time"]),
                    str(row["minutes_until_release"]),
                    str(row["timing"]),
                    str(row["source"]),
                    _escape_md(str(row["title"])),
                ]
            )
            + " |"
        )
    if not payload["release_window"]["release_window_events"]:
        lines.append("| n/a | n/a | 0 | outside window | n/a | No current release window |")
    lines.extend(
        [
            "",
            "## Source State",
            "",
            f"- Source configured: {summary['source_configured']}",
            "- Trading Economics API configured: "
            f"{source_state['trading_economics_api_configured']}",
            "- Verified input file configured: "
            f"{source_state['verified_input_file_configured']}",
            "- Credential value reported: false",
            "",
            "## Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def _event_label(event_key: str | None, event_time: str | None) -> str:
    if not event_key or not event_time:
        return "n/a"
    return f"{event_key} at {event_time}"


def _escape_md(value: str) -> str:
    return value.replace("|", "\\|")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
