from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import INACTIVE_MARKET_STATUSES
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import database_url_from_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    MarketSnapshot,
    WeatherFeature,
    WeatherMarketLink,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now
from kalshi_predictor.weather.linker import WEATHER_TICKER_PREFIXES, detect_weather_market
from kalshi_predictor.weather.repository import insert_weather_market_link

PHASE_3AZ_R12_WEATHER_VERSION = "phase3az_r12_weather_v1"
PHASE_3AZ_R13_WEATHER_VERSION = "phase3az_r13_weather_handoff_v1"


@dataclass(frozen=True)
class Phase3AZR12WeatherArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    next_actions_path: Path
    candidates_csv_path: Path
    safe_to_relink_csv_path: Path
    safe_to_link_csv_path: Path


@dataclass(frozen=True)
class Phase3AZR12WeatherApplyArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3AZR13WeatherHandoffArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    next_actions_path: Path


def write_phase3az_r12_weather_activation_preview_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3az_r12_weather"),
    limit: int = 1000,
    fresh_window_hours: int | None = None,
    match_tolerance_hours: int = 3,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3AZR12WeatherArtifactSet:
    payload = build_phase3az_r12_weather_activation_preview(
        session,
        limit=limit,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "weather_activation_preview.json"
    markdown_path = output_dir / "weather_activation_preview.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    candidates_csv_path = output_dir / "weather_activation_candidates.csv"
    safe_to_relink_csv_path = output_dir / "safe_to_relink.csv"
    safe_to_link_csv_path = output_dir / "safe_to_link.csv"

    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_csv(candidates_csv_path, payload["candidate_rows"])
    _write_csv(
        safe_to_relink_csv_path,
        [row for row in payload["candidate_rows"] if row["safe_to_relink"]],
    )
    _write_csv(
        safe_to_link_csv_path,
        [row for row in payload["candidate_rows"] if row["safe_to_link"]],
    )
    return Phase3AZR12WeatherArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        next_actions_path=next_actions_path,
        candidates_csv_path=candidates_csv_path,
        safe_to_relink_csv_path=safe_to_relink_csv_path,
        safe_to_link_csv_path=safe_to_link_csv_path,
    )


def write_phase3az_r12_weather_missing_link_apply_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3az_r12_weather"),
    limit: int = 1000,
    fresh_window_hours: int | None = None,
    match_tolerance_hours: int = 3,
    max_records: int = 25,
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3AZR12WeatherApplyArtifactSet:
    payload = build_phase3az_r12_weather_missing_link_apply(
        session,
        output_dir=output_dir,
        limit=limit,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        max_records=max_records,
        dry_run=dry_run,
        apply=apply,
        backup_first=backup_first,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "weather_missing_link_apply.json"
    markdown_path = output_dir / "weather_missing_link_apply.md"
    json_path = _write_text_with_permission_fallback(
        json_path,
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path = _write_text_with_permission_fallback(
        markdown_path,
        _render_apply_markdown(payload),
        encoding="utf-8",
    )
    return Phase3AZR12WeatherApplyArtifactSet(output_dir, json_path, markdown_path)


def _write_text_with_permission_fallback(path: Path, text: str, *, encoding: str) -> Path:
    try:
        path.write_text(text, encoding=encoding)
        return path
    except PermissionError:
        fallback_path = path.with_name(f"{path.stem}_{_timestamp_for_path()}{path.suffix}")
        fallback_path.write_text(text, encoding=encoding)
        return fallback_path


def write_phase3az_r13_weather_handoff_status_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3az_r13_weather"),
    reports_dir: Path = Path("reports"),
    current_window_lookback_hours: int = 3,
    limit: int = 500,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3AZR13WeatherHandoffArtifactSet:
    payload = build_phase3az_r13_weather_handoff_status(
        session,
        reports_dir=reports_dir,
        current_window_lookback_hours=current_window_lookback_hours,
        limit=limit,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "weather_handoff_status.json"
    markdown_path = output_dir / "weather_handoff_status.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_handoff_markdown(payload), encoding="utf-8")
    next_actions_path.write_text(_render_handoff_next_actions(payload), encoding="utf-8")
    return Phase3AZR13WeatherHandoffArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        next_actions_path=next_actions_path,
    )


def build_phase3az_r12_weather_activation_preview(
    session: Session,
    *,
    limit: int = 1000,
    fresh_window_hours: int | None = None,
    match_tolerance_hours: int = 3,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    max_age_hours = int(
        fresh_window_hours
        if fresh_window_hours is not None
        else resolved_settings.weather_v2_max_forecast_age_hours
    )
    now = utc_now()
    feature_rows = _fresh_features_by_location(
        session,
        now=now,
        max_age_hours=max_age_hours,
    )
    markets = _weather_market_candidates(session, limit=limit)
    links_by_ticker = _latest_links_by_ticker(session, [market.ticker for market in markets])
    min_confidence = (
        to_decimal(resolved_settings.weather_v2_min_link_confidence) or Decimal("0.6")
    )
    candidate_rows = [
        _candidate_row(
            market,
            links_by_ticker.get(market.ticker),
            fresh_features_by_location=feature_rows,
            now=now,
            min_confidence=min_confidence,
            max_age_hours=max_age_hours,
            match_tolerance_hours=match_tolerance_hours,
        )
        for market in markets
    ]
    safe_rows = [row for row in candidate_rows if row["safe_to_relink"]]
    safe_link_rows = [row for row in candidate_rows if row["safe_to_link"]]
    stale_rows = [row for row in candidate_rows if row["stale_target_time_link"]]
    current_linkable = [row for row in candidate_rows if row["current_linkable_weather_ticker"]]
    stale_not_safe = [
        row for row in candidate_rows if row["stale_target_time_link"] and not row["safe_to_relink"]
    ]
    return {
        "generated_at": now.isoformat(),
        "phase": "3AZ-R12",
        "phase_version": PHASE_3AZ_R12_WEATHER_VERSION,
        "mode": "PAPER_ONLY_WEATHER_ACTIVATION_DIAGNOSTIC_RELINK_PREVIEW",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "safety_flags": {
            "db_writes": "blocked",
            "paper_trade_creation": "blocked",
            "live_demo_orders": "blocked",
            "threshold_relaxation": "blocked",
            "normal_link_remediation": "blocked",
        },
        "command_args": command_args or [],
        "parameters": {
            "limit": limit,
            "fresh_window_hours": max_age_hours,
            "match_tolerance_hours": match_tolerance_hours,
            "min_link_confidence": str(min_confidence),
        },
        "fresh_feature_windows": _feature_window_summary(feature_rows, now=now),
        "summary": {
            "active_weather_markets_reviewed": len(candidate_rows),
            "stale_target_time_links": len(stale_rows),
            "current_linkable_weather_tickers": len(current_linkable),
            "rows_safe_to_relink": len(safe_rows),
            "rows_safe_to_link": len(safe_link_rows),
            "stale_target_time_links_not_safe": len(stale_not_safe),
            "missing_weather_links": sum(
                1 for row in candidate_rows if not row["has_existing_link"]
            ),
            "missing_weather_links_safe_to_link": len(safe_link_rows),
            "no_fresh_window_rows": sum(
                1 for row in candidate_rows if row["blocker"] == "NO_FRESH_FORECAST_WINDOW"
            ),
            "expired_target_rows": sum(
                1 for row in candidate_rows if row["blocker"] == "TARGET_TIME_NOT_CURRENT"
            ),
            "first_blocker": _first_blocker(candidate_rows),
            "ready_to_run_weather_sprint": len(safe_rows) > 0,
            "ready_for_missing_link_apply": len(safe_link_rows) > 0,
            "should_rerun_same_weather_sprint": len(safe_rows) > 0,
        },
        "candidate_rows": candidate_rows,
        "operator_guardrails": [
            "Do not rerun the same weather sprint until rows_safe_to_relink is greater than 0.",
            "Run missing-link apply only if rows_safe_to_link is greater than 0.",
            "Do not write weather_market_links from this diagnostic.",
            "Do not create paper trades unless a downstream paper-ready gate opens.",
        ],
        "recommended_next_action": _recommended_next_action(
            rows_safe_to_relink=len(safe_rows),
            rows_safe_to_link=len(safe_link_rows),
        ),
    }


def build_phase3az_r13_weather_handoff_status(
    session: Session,
    *,
    reports_dir: Path = Path("reports"),
    current_window_lookback_hours: int = 3,
    limit: int = 500,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    current_since = now - timedelta(hours=max(current_window_lookback_hours, 0))
    db_url = _session_db_url(session) or database_url_from_settings(resolved)
    writer = db_writer_monitor(settings=resolved, db_url=db_url)
    preview_summary = _latest_r12_preview_summary(reports_dir)
    rows = _weather_handoff_rows(
        session,
        current_since=current_since,
        model_name="weather_v2",
        limit=limit,
    )
    summary = _weather_handoff_summary(
        rows,
        preview_summary=preview_summary,
        writer=writer,
    )
    next_action = _weather_handoff_next_action(summary)
    return {
        "generated_at": now.isoformat(),
        "phase": "3AZ-R13",
        "phase_version": PHASE_3AZ_R13_WEATHER_VERSION,
        "mode": "PAPER_ONLY_WEATHER_HANDOFF_STATUS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "safety_flags": {
            "db_writes": "blocked",
            "paper_trade_creation": "blocked",
            "live_demo_orders": "blocked",
            "forecast_run": "blocked",
            "opportunity_scan": "blocked",
        },
        "command_args": command_args or [],
        "parameters": {
            "current_window_lookback_hours": current_window_lookback_hours,
            "limit": limit,
            "forecast_model": "weather_v2",
            "reports_dir": str(reports_dir),
        },
        "active_db_writer_status": writer,
        "r12_preview_summary": preview_summary,
        "summary": summary,
        "next_action": next_action,
        "handoff_rows": rows,
        "operator_guardrails": [
            "This command is report-only and safe to run while another DB writer is active.",
            (
                "Run write-capable commands only when db-writer-monitor reports "
                "safe_to_start_write=true."
            ),
            "Do not create paper trades unless phase3ap-paper-ready-unblock-report opens the gate.",
        ],
    }


def build_phase3az_r12_weather_missing_link_apply(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3az_r12_weather"),
    limit: int = 1000,
    fresh_window_hours: int | None = None,
    match_tolerance_hours: int = 3,
    max_records: int = 25,
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    if apply and dry_run:
        raise ValueError("apply=true requires dry_run=false.")
    if apply and not backup_first:
        raise ValueError("--apply requires --backup-first.")

    resolved = settings or get_settings()
    preview = build_phase3az_r12_weather_activation_preview(
        session,
        limit=limit,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        settings=resolved,
        command_args=command_args,
    )
    candidates = [row for row in preview["candidate_rows"] if row["safe_to_link"]]
    candidates = candidates[: max(max_records, 0)]
    db_url = _session_db_url(session) or database_url_from_settings(resolved)
    writer = db_writer_monitor(settings=resolved, db_url=db_url)
    status = "DRY_RUN"
    blocked_reason = None
    backup_path = None
    written_rows: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []

    if apply and _writer_blocks_apply(writer):
        status = "BLOCKED_BY_ACTIVE_WRITER"
        blocked_reason = "Another DB writer owns the database."
    elif apply and not candidates:
        status = "NO_SAFE_ROWS"
    elif apply:
        output_dir.mkdir(parents=True, exist_ok=True)
        backup_path = _write_missing_link_logical_backup(
            session,
            output_dir=output_dir,
            candidates=candidates,
        )
        for row in candidates:
            market = session.get(Market, str(row["ticker"]))
            if market is None:
                skipped_rows.append({"ticker": row["ticker"], "reason": "MARKET_NOT_FOUND"})
                continue
            existing = session.scalar(
                select(WeatherMarketLink)
                .where(WeatherMarketLink.ticker == market.ticker)
                .order_by(desc(WeatherMarketLink.detected_at), desc(WeatherMarketLink.id))
                .limit(1)
            )
            if existing is not None:
                skipped_rows.append({"ticker": row["ticker"], "reason": "LINK_ALREADY_EXISTS"})
                continue
            detection = detect_weather_market(market)
            inserted = insert_weather_market_link(
                session,
                ticker=market.ticker,
                location_key=detection.location_key,
                weather_metric=detection.weather_metric,
                target_operator=detection.target_operator,
                target_value=detection.target_value,
                target_time=detection.target_time,
                confidence=detection.confidence,
                reason="phase3az_r12_missing_current_weather_link_apply",
                raw_json={
                    "source": "phase3az_r12_weather_missing_link_apply",
                    "market_title": market.title,
                    "market_subtitle": market.subtitle,
                    "series_ticker": market.series_ticker,
                    "event_ticker": market.event_ticker,
                    "fresh_feature_id": row.get("fresh_feature_id"),
                    "fresh_feature_target_time": row.get("fresh_feature_target_time"),
                    "candidate_row": row,
                    "raw_market": decode_json(market.raw_json),
                },
            )
            written_rows.append(
                {
                    "ticker": market.ticker,
                    "link_id": inserted.id,
                    "location_key": inserted.location_key,
                    "weather_metric": inserted.weather_metric,
                    "target_operator": inserted.target_operator,
                    "target_value": inserted.target_value,
                    "target_time": _iso(inserted.target_time),
                }
            )
        session.flush()
        session.commit()
        status = "APPLIED" if written_rows else "NO_ROWS_WRITTEN"

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AZ-R12",
        "phase_version": PHASE_3AZ_R12_WEATHER_VERSION,
        "mode": "WEATHER_MISSING_LINK_APPLY" if apply else "WEATHER_MISSING_LINK_DRY_RUN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "safety_flags": {
            "paper_trade_creation": "blocked",
            "live_demo_orders": "blocked",
            "weather_forecast_run": "blocked",
            "weather_ingest_run": "blocked",
            "weather_feature_build_run": "blocked",
        },
        "command_args": command_args or [],
        "dry_run": dry_run,
        "apply": apply,
        "backup_first": backup_first,
        "backup_path": str(backup_path) if backup_path is not None else None,
        "backup_kind": "LOGICAL_WEATHER_MARKET_LINK_ROWS"
        if backup_path is not None
        else None,
        "active_db_writer_status": writer,
        "status": status,
        "blocked_reason": blocked_reason,
        "summary": {
            "preview_rows_safe_to_link": int(preview["summary"]["rows_safe_to_link"]),
            "candidates_reviewed": len(candidates),
            "would_write_link_rows": len(candidates) if not apply else 0,
            "link_rows_written": len(written_rows),
            "skipped_rows": len(skipped_rows),
        },
        "candidate_rows": candidates,
        "written_rows": written_rows,
        "skipped_rows": skipped_rows,
    }


def _weather_market_candidates(session: Session, *, limit: int) -> list[Market]:
    tickers = _weather_candidate_tickers(session, limit=limit)
    if not tickers:
        return []
    statement = (
        select(Market)
        .where(Market.ticker.in_(tickers))
        .where(
            or_(
                Market.status.is_(None),
                ~func.lower(Market.status).in_(tuple(sorted(INACTIVE_MARKET_STATUSES))),
            )
        )
        .order_by(Market.ticker)
    )
    if limit > 0:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def _weather_candidate_tickers(session: Session, *, limit: int) -> list[str]:
    max_rows = limit if limit > 0 else 5000
    tickers: list[str] = []
    seen: set[str] = set()

    def add(values: list[str]) -> None:
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            tickers.append(value)
            if len(tickers) >= max_rows:
                break

    add(
        list(
            session.scalars(
                select(MarketLeg.ticker)
                .where(MarketLeg.category == "weather")
                .distinct()
                .order_by(MarketLeg.ticker)
                .limit(max_rows)
            )
        )
    )
    if len(tickers) >= max_rows:
        return tickers

    add(
        list(
            session.scalars(
                select(WeatherMarketLink.ticker)
                .distinct()
                .order_by(WeatherMarketLink.ticker)
                .limit(max_rows)
            )
        )
    )
    if len(tickers) >= max_rows:
        return tickers

    ticker_family_filters = [
        Market.ticker.like(f"{prefix}%") for prefix in WEATHER_TICKER_PREFIXES
    ] + [
        Market.series_ticker.like(f"{prefix}%") for prefix in WEATHER_TICKER_PREFIXES
    ]
    add(
        list(
            session.scalars(
                select(Market.ticker)
                .where(or_(*ticker_family_filters))
                .where(
                    or_(
                        Market.status.is_(None),
                        ~func.lower(Market.status).in_(
                            tuple(sorted(INACTIVE_MARKET_STATUSES))
                        ),
                    )
                )
                .order_by(Market.ticker)
                .limit(max_rows)
            )
        )
    )
    return tickers


def _weather_handoff_rows(
    session: Session,
    *,
    current_since,
    model_name: str,
    limit: int,
) -> list[dict[str, Any]]:
    links = _current_weather_links(session, current_since=current_since, limit=limit)
    rows: list[dict[str, Any]] = []
    for link in links:
        snapshot = _latest_snapshot_for_ticker(session, link.ticker)
        forecast = _latest_forecast_for_ticker(session, link.ticker, model_name=model_name)
        ranking = _latest_ranking_for_ticker(session, link.ticker, model_name=model_name)
        snapshot_at = _as_utc(snapshot.captured_at) if snapshot is not None else None
        forecast_at = _as_utc(forecast.forecasted_at) if forecast is not None else None
        ranking_at = _as_utc(ranking.ranked_at) if ranking is not None else None
        has_current_forecast = bool(
            forecast_at and (snapshot_at is None or forecast_at >= snapshot_at)
        )
        has_current_ranking = bool(ranking_at and forecast_at and ranking_at >= forecast_at)
        rows.append(
            {
                "ticker": link.ticker,
                "location_key": link.location_key,
                "target_time": _iso(link.target_time),
                "link_detected_at": _iso(link.detected_at),
                "latest_snapshot_at": _iso(snapshot_at),
                "latest_forecast_at": _iso(forecast_at),
                "latest_ranking_at": _iso(ranking_at),
                "has_snapshot": snapshot is not None,
                "has_current_forecast": has_current_forecast,
                "has_current_ranking": has_current_ranking,
                "estimated_edge": ranking.estimated_edge if ranking is not None else None,
                "opportunity_score": ranking.opportunity_score if ranking is not None else None,
            }
        )
    return rows


def _current_weather_links(
    session: Session,
    *,
    current_since,
    limit: int,
) -> list[WeatherMarketLink]:
    statement = (
        select(WeatherMarketLink)
        .where(WeatherMarketLink.target_time.is_not(None))
        .where(WeatherMarketLink.target_time >= current_since)
        .order_by(
            desc(WeatherMarketLink.target_time),
            WeatherMarketLink.ticker,
            desc(WeatherMarketLink.detected_at),
            desc(WeatherMarketLink.id),
        )
        .limit(max(limit * 3, limit, 1))
    )
    latest: dict[str, WeatherMarketLink] = {}
    for link in session.scalars(statement):
        latest.setdefault(link.ticker, link)
        if len(latest) >= limit:
            break
    return list(latest.values())


def _latest_snapshot_for_ticker(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_forecast_for_ticker(
    session: Session,
    ticker: str,
    *,
    model_name: str,
) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == model_name)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _latest_ranking_for_ticker(
    session: Session,
    ticker: str,
    *,
    model_name: str,
) -> MarketRanking | None:
    return session.scalar(
        select(MarketRanking)
        .where(MarketRanking.ticker == ticker, MarketRanking.forecast_model == model_name)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(1)
    )


def _weather_handoff_summary(
    rows: list[dict[str, Any]],
    *,
    preview_summary: dict[str, Any],
    writer: dict[str, Any],
) -> dict[str, Any]:
    links = len(rows)
    with_snapshot = sum(1 for row in rows if row["has_snapshot"])
    with_forecast = sum(1 for row in rows if row["has_current_forecast"])
    with_ranking = sum(1 for row in rows if row["has_current_ranking"])
    return {
        "r12_rows_safe_to_link": int(preview_summary.get("rows_safe_to_link") or 0),
        "r12_rows_safe_to_relink": int(preview_summary.get("rows_safe_to_relink") or 0),
        "current_weather_links": links,
        "links_with_snapshots": with_snapshot,
        "links_with_current_weather_forecasts": with_forecast,
        "links_with_current_weather_rankings": with_ranking,
        "snapshot_gap_rows": max(links - with_snapshot, 0),
        "forecast_gap_rows": sum(
            1 for row in rows if row["has_snapshot"] and not row["has_current_forecast"]
        ),
        "ranking_gap_rows": sum(
            1 for row in rows if row["has_current_forecast"] and not row["has_current_ranking"]
        ),
        "db_writer_safe_to_start": bool(writer.get("safe_to_start_write", True)),
        "active_writer_pid": (writer.get("current_writer") or {}).get("pid")
        if isinstance(writer.get("current_writer"), dict)
        else writer.get("current_writer_pid"),
    }


def _weather_handoff_next_action(summary: dict[str, Any]) -> dict[str, Any]:
    writer_safe = bool(summary.get("db_writer_safe_to_start", True))
    if summary["r12_rows_safe_to_link"] > 0:
        command = (
            "kalshi-bot phase3az-r12-weather-missing-link-apply --output-dir "
            "reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 "
            "--match-tolerance-hours 3 --max-records 25 --apply --backup-first"
        )
        return _handoff_action("APPLY_SAFE_MISSING_LINKS", command, writer_safe=writer_safe)
    if summary["ranking_gap_rows"] > 0:
        command = (
            "kalshi-bot find-opportunities --model-name weather_v2 --limit 100 "
            "--output reports/weather_opportunities.md"
        )
        return _handoff_action(
            "INSERT_WEATHER_OPPORTUNITY_RANKINGS",
            command,
            writer_safe=writer_safe,
        )
    if summary["forecast_gap_rows"] > 0:
        command = "kalshi-bot forecast --model weather_v2 --limit 500"
        return _handoff_action("RUN_WEATHER_FORECAST", command, writer_safe=writer_safe)
    if summary["snapshot_gap_rows"] > 0:
        command = (
            "kalshi-bot snapshot --status open --limit 100 --max-pages 3 "
            "--series-ticker KXTEMPNYCH"
        )
        return _handoff_action(
            "CAPTURE_TARGETED_WEATHER_SNAPSHOTS",
            command,
            writer_safe=writer_safe,
        )
    if summary["links_with_current_weather_rankings"] > 0:
        return {
            "stage": "RUN_PAPER_READY_GATE",
            "command": (
                "kalshi-bot phase3ap-paper-ready-unblock-report --output-dir "
                "reports/phase3ap --reports-dir reports"
            ),
            "writer_required": False,
            "blocked_by_writer": False,
            "reason": "Current weather rankings exist; run the canonical paper-ready gate.",
        }
    return {
        "stage": "REFRESH_NEXT_WEATHER_WINDOW",
        "command": (
            "kalshi-bot sync-markets --status open --limit 100 --max-pages 3 "
            "--series-ticker KXTEMPNYCH"
        ),
        "writer_required": True,
        "blocked_by_writer": not writer_safe,
        "reason": "No current linked weather handoff rows are ready.",
    }


def _handoff_action(stage: str, command: str, *, writer_safe: bool) -> dict[str, Any]:
    return {
        "stage": stage,
        "command": command,
        "writer_required": True,
        "blocked_by_writer": not writer_safe,
        "reason": "Writer gate must be clear before running this write-capable step.",
    }


def _latest_r12_preview_summary(reports_dir: Path) -> dict[str, Any]:
    path = reports_dir / "phase3az_r12_weather" / "weather_activation_preview.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    summary = payload.get("summary")
    return dict(summary) if isinstance(summary, dict) else {}


def _latest_links_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, WeatherMarketLink]:
    if not tickers:
        return {}
    links = list(
        session.scalars(
            select(WeatherMarketLink)
            .where(WeatherMarketLink.ticker.in_(tickers))
            .order_by(
                WeatherMarketLink.ticker,
                desc(WeatherMarketLink.detected_at),
                desc(WeatherMarketLink.id),
            )
        )
    )
    latest: dict[str, WeatherMarketLink] = {}
    for link in links:
        latest.setdefault(link.ticker, link)
    return latest


def _fresh_features_by_location(
    session: Session,
    *,
    now,
    max_age_hours: int,
    feature_limit: int = 20000,
) -> dict[str, list[WeatherFeature]]:
    earliest_generated_at = now - timedelta(hours=max(max_age_hours + 6, 12))
    features = list(
        session.scalars(
            select(WeatherFeature)
            .where(WeatherFeature.generated_at >= earliest_generated_at)
            .order_by(
                desc(WeatherFeature.generated_at),
                WeatherFeature.location_key,
                WeatherFeature.target_time,
            )
            .limit(feature_limit)
        )
    )
    by_location: dict[str, list[WeatherFeature]] = {}
    for feature in features:
        age_hours = _feature_age_hours(feature, now=now)
        target_time = _as_utc(feature.target_time)
        if age_hours is None or target_time is None:
            continue
        if age_hours > Decimal(str(max_age_hours)):
            continue
        by_location.setdefault(feature.location_key, []).append(feature)
    return by_location


def _candidate_row(
    market: Market,
    link: WeatherMarketLink | None,
    *,
    fresh_features_by_location: dict[str, list[WeatherFeature]],
    now,
    min_confidence: Decimal,
    max_age_hours: int,
    match_tolerance_hours: int,
) -> dict[str, Any]:
    detection = detect_weather_market(market)
    detected_target_time = _as_utc(detection.target_time)
    linked_target_time = _as_utc(link.target_time) if link is not None else None
    detected_location = detection.location_key
    detected_confidence = to_decimal(detection.confidence) or Decimal("0")
    target_time_current = detected_target_time is not None and detected_target_time >= now
    detected_text_complete = (
        detection.weather_metric != "UNKNOWN"
        and detected_location != "unknown"
        and detection.target_operator != "UNKNOWN"
        and detection.target_value is not None
    )
    matched_feature = _nearest_fresh_feature(
        fresh_features_by_location.get(detected_location, []),
        target_time=detected_target_time,
        max_tolerance_hours=match_tolerance_hours,
    )
    current_linkable = (
        detected_confidence >= min_confidence
        and detected_text_complete
        and target_time_current
        and matched_feature is not None
    )
    stale_link = _stale_target_time_link(
        link,
        fresh_features_by_location=fresh_features_by_location,
        now=now,
        max_age_hours=max_age_hours,
        match_tolerance_hours=match_tolerance_hours,
    )
    link_fields_match = _link_fields_match(link, detection)
    safe_to_relink = bool(stale_link and current_linkable and link_fields_match)
    safe_to_link = bool(link is None and current_linkable)
    blocker = _blocker(
        safe_to_relink=safe_to_relink,
        safe_to_link=safe_to_link,
        link=link,
        stale_link=stale_link,
        current_linkable=current_linkable,
        detected_text_complete=detected_text_complete,
        target_time_current=target_time_current,
        matched_feature=matched_feature,
        detected_confidence=detected_confidence,
        min_confidence=min_confidence,
        link_fields_match=link_fields_match,
    )
    return {
        "ticker": market.ticker,
        "status": market.status,
        "title": market.title,
        "series_ticker": market.series_ticker,
        "has_existing_link": link is not None,
        "existing_link_id": link.id if link is not None else None,
        "existing_location_key": link.location_key if link is not None else None,
        "existing_metric": link.weather_metric if link is not None else None,
        "existing_operator": link.target_operator if link is not None else None,
        "existing_target_value": link.target_value if link is not None else None,
        "existing_target_time": _iso(linked_target_time),
        "detected_location_key": detected_location,
        "detected_metric": detection.weather_metric,
        "detected_operator": detection.target_operator,
        "detected_target_value": str(detection.target_value)
        if detection.target_value is not None
        else None,
        "detected_target_time": _iso(detected_target_time),
        "detected_confidence": str(detected_confidence),
        "stale_target_time_link": stale_link,
        "current_linkable_weather_ticker": current_linkable,
        "fresh_feature_id": matched_feature.id if matched_feature is not None else None,
        "fresh_feature_target_time": _iso(_as_utc(matched_feature.target_time))
        if matched_feature is not None
        else None,
        "fresh_feature_age_hours": str(_feature_age_hours(matched_feature, now=now))
        if matched_feature is not None
        else None,
        "link_fields_match_current_text": link_fields_match,
        "safe_to_relink": safe_to_relink,
        "safe_to_link": safe_to_link,
        "blocker": blocker,
    }


def _stale_target_time_link(
    link: WeatherMarketLink | None,
    *,
    fresh_features_by_location: dict[str, list[WeatherFeature]],
    now,
    max_age_hours: int,
    match_tolerance_hours: int,
) -> bool:
    if link is None:
        return False
    linked_target_time = _as_utc(link.target_time)
    if linked_target_time is None:
        return True
    if linked_target_time < now:
        return True
    matched_feature = _nearest_fresh_feature(
        fresh_features_by_location.get(link.location_key, []),
        target_time=linked_target_time,
        max_tolerance_hours=match_tolerance_hours,
    )
    if matched_feature is None:
        return True
    age_hours = _feature_age_hours(matched_feature, now=now)
    return age_hours is None or age_hours > Decimal(str(max_age_hours))


def _nearest_fresh_feature(
    features: list[WeatherFeature],
    *,
    target_time,
    max_tolerance_hours: int,
) -> WeatherFeature | None:
    target = _as_utc(target_time)
    if target is None or not features:
        return None

    def distance_hours(feature: WeatherFeature) -> float:
        feature_target = _as_utc(feature.target_time)
        if feature_target is None:
            return float("inf")
        return abs((feature_target - target).total_seconds()) / 3600

    closest = min(features, key=distance_hours)
    return closest if distance_hours(closest) <= max_tolerance_hours else None


def _link_fields_match(
    link: WeatherMarketLink | None,
    detection,
) -> bool:
    if link is None:
        return False
    return (
        _norm(link.location_key) == _norm(detection.location_key)
        and _norm(link.weather_metric) == _norm(detection.weather_metric)
        and _norm(link.target_operator) == _norm(detection.target_operator)
        and _same_decimal(link.target_value, detection.target_value)
    )


def _blocker(
    *,
    safe_to_relink: bool,
    safe_to_link: bool,
    link: WeatherMarketLink | None,
    stale_link: bool,
    current_linkable: bool,
    detected_text_complete: bool,
    target_time_current: bool,
    matched_feature: WeatherFeature | None,
    detected_confidence: Decimal,
    min_confidence: Decimal,
    link_fields_match: bool,
) -> str:
    if safe_to_relink:
        return "SAFE_TO_RELINK"
    if safe_to_link:
        return "SAFE_TO_LINK"
    if link is None:
        return "MISSING_EXISTING_WEATHER_LINK"
    if not stale_link:
        return "CURRENT_LINK_NOT_STALE"
    if detected_confidence < min_confidence:
        return "LOW_LINK_CONFIDENCE"
    if not detected_text_complete:
        return "MARKET_TEXT_NOT_SAFE_TO_RELINK"
    if not target_time_current:
        return "TARGET_TIME_NOT_CURRENT"
    if matched_feature is None:
        return "NO_FRESH_FORECAST_WINDOW"
    if not link_fields_match:
        return "LINK_METADATA_DIFFERS_FROM_CURRENT_TEXT"
    if not current_linkable:
        return "CURRENT_WEATHER_TICKER_NOT_LINKABLE"
    return "STALE_LINK_NOT_SAFE_TO_RELINK"


def _feature_window_summary(
    feature_rows: dict[str, list[WeatherFeature]],
    *,
    now,
) -> list[dict[str, Any]]:
    rows = []
    for location_key, features in sorted(feature_rows.items()):
        target_times = sorted(_as_utc(feature.target_time) for feature in features)
        target_times = [target for target in target_times if target is not None]
        ages = [_feature_age_hours(feature, now=now) for feature in features]
        ages = [age for age in ages if age is not None]
        rows.append(
            {
                "location_key": location_key,
                "fresh_feature_rows": len(features),
                "first_target_time": _iso(target_times[0]) if target_times else None,
                "last_target_time": _iso(target_times[-1]) if target_times else None,
                "max_forecast_age_hours": str(max(ages)) if ages else None,
            }
        )
    return rows


def _feature_age_hours(feature: WeatherFeature, *, now) -> Decimal | None:
    raw = decode_json(feature.raw_json)
    explicit_age = to_decimal(raw.get("forecast_age_hours"))
    if explicit_age is not None:
        return explicit_age
    generated_at = parse_datetime(raw.get("forecast_generated_at")) or _as_utc(feature.generated_at)
    if generated_at is None:
        return None
    age = Decimal(str((now - generated_at).total_seconds() / 3600))
    return max(age, Decimal("0"))


def _first_blocker(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        if row["blocker"] not in {"SAFE_TO_RELINK", "SAFE_TO_LINK"}:
            return str(row["blocker"])
    return "NONE"


def _recommended_next_action(*, rows_safe_to_relink: int, rows_safe_to_link: int) -> str:
    if rows_safe_to_link > 0:
        return (
            "Run a writer-gated missing-link apply for safe_to_link.csv, then rerun this "
            "preview. Do not run weather forecasts yet."
        )
    if rows_safe_to_relink > 0:
        return (
            "Review safe_to_relink.csv, then build/run a separate write-gated relink apply. "
            "Do not create paper trades until phase3ap opens."
        )
    return (
        "Do not rerun the same weather sprint yet. Refresh active Kalshi weather market "
        "catalog/parsing first, or wait for current weather tickers with fresh forecast windows."
    )


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AZ-R12 Weather Activation Preview",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "Mode: PAPER ONLY diagnostic/relink preview. No database writes or orders were run.",
        "",
        "## Summary",
        "",
        f"- Active weather markets reviewed: {summary['active_weather_markets_reviewed']}",
        f"- Stale target-time links: {summary['stale_target_time_links']}",
        f"- Current linkable weather tickers: {summary['current_linkable_weather_tickers']}",
        f"- rows_safe_to_relink: {summary['rows_safe_to_relink']}",
        f"- rows_safe_to_link: {summary['rows_safe_to_link']}",
        f"- First blocker: {summary['first_blocker']}",
        "",
        "## Next Action",
        "",
        payload["recommended_next_action"],
        "",
        "## Fresh Feature Windows",
        "",
        "| Location | Fresh Rows | First Target | Last Target | Max Age Hours |",
        "|---|---:|---|---|---:|",
    ]
    for row in payload["fresh_feature_windows"]:
        lines.append(
            "| {location_key} | {fresh_feature_rows} | {first_target_time} | "
            "{last_target_time} | {max_forecast_age_hours} |".format(**row)
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    rows_safe_relink = int(payload["summary"]["rows_safe_to_relink"])
    rows_safe_link = int(payload["summary"]["rows_safe_to_link"])
    lines = [
        "# Next Actions",
        "",
        f"rows_safe_to_relink={rows_safe_relink}",
        f"rows_safe_to_link={rows_safe_link}",
        "",
        payload["recommended_next_action"],
        "",
        "Guardrails:",
    ]
    lines.extend(f"- {item}" for item in payload["operator_guardrails"])
    return "\n".join(lines) + "\n"


def _render_apply_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AZ-R12 Weather Missing Link Apply",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        f"Status: `{payload['status']}`",
        f"Dry run: `{payload['dry_run']}`",
        f"Apply: `{payload['apply']}`",
        f"Backup: `{payload['backup_path']}`",
        "",
        "## Summary",
        "",
        f"- preview_rows_safe_to_link: {summary['preview_rows_safe_to_link']}",
        f"- candidates_reviewed: {summary['candidates_reviewed']}",
        f"- would_write_link_rows: {summary['would_write_link_rows']}",
        f"- link_rows_written: {summary['link_rows_written']}",
        f"- skipped_rows: {summary['skipped_rows']}",
        "",
        "Weather ingest, feature build, forecast, paper trade creation, and live/demo "
        "orders were not run.",
    ]
    return "\n".join(lines) + "\n"


def _render_handoff_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    action = payload["next_action"]
    lines = [
        "# Phase 3AZ-R13 Weather Handoff Status",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "Mode: PAPER ONLY report-only diagnostic",
        "Database writes: blocked",
        "Paper trade creation: blocked",
        "Live/demo execution: blocked",
        "",
        "## Summary",
        "",
        f"- r12_rows_safe_to_link: {summary['r12_rows_safe_to_link']}",
        f"- current_weather_links: {summary['current_weather_links']}",
        f"- links_with_snapshots: {summary['links_with_snapshots']}",
        "- links_with_current_weather_forecasts: "
        f"{summary['links_with_current_weather_forecasts']}",
        "- links_with_current_weather_rankings: "
        f"{summary['links_with_current_weather_rankings']}",
        f"- snapshot_gap_rows: {summary['snapshot_gap_rows']}",
        f"- forecast_gap_rows: {summary['forecast_gap_rows']}",
        f"- ranking_gap_rows: {summary['ranking_gap_rows']}",
        f"- db_writer_safe_to_start: {summary['db_writer_safe_to_start']}",
        "",
        "## Next Action",
        "",
        f"- Stage: `{action['stage']}`",
        f"- Blocked by writer: `{action['blocked_by_writer']}`",
        f"- Command: `{action['command']}`",
        f"- Reason: {action['reason']}",
        "",
        "## Guardrails",
    ]
    lines.extend(f"- {item}" for item in payload["operator_guardrails"])
    return "\n".join(lines) + "\n"


def _render_handoff_next_actions(payload: dict[str, Any]) -> str:
    action = payload["next_action"]
    lines = [
        "# Phase 3AZ-R13 Weather Next Action",
        "",
        f"Stage: `{action['stage']}`",
        f"Blocked by writer: `{action['blocked_by_writer']}`",
        "",
        action["command"],
        "",
        "Do not create paper trades unless phase3ap-paper-ready-unblock-report opens the gate.",
    ]
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker",
        "status",
        "has_existing_link",
        "existing_link_id",
        "existing_location_key",
        "existing_metric",
        "existing_operator",
        "existing_target_value",
        "existing_target_time",
        "detected_location_key",
        "detected_metric",
        "detected_operator",
        "detected_target_value",
        "detected_target_time",
        "stale_target_time_link",
        "current_linkable_weather_ticker",
        "fresh_feature_id",
        "fresh_feature_target_time",
        "fresh_feature_age_hours",
        "link_fields_match_current_text",
        "safe_to_relink",
        "safe_to_link",
        "blocker",
        "title",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_missing_link_logical_backup(
    session: Session,
    *,
    output_dir: Path,
    candidates: list[dict[str, Any]],
) -> Path:
    tickers = [str(row["ticker"]) for row in candidates]
    backup_dir = output_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_name = f"phase3az_r12_weather_missing_link_{_timestamp_for_path()}.json"
    backup_path = backup_dir / backup_name
    markets = list(
        session.scalars(select(Market).where(Market.ticker.in_(tickers)).order_by(Market.ticker))
    )
    existing_links = list(
        session.scalars(
            select(WeatherMarketLink)
            .where(WeatherMarketLink.ticker.in_(tickers))
            .order_by(WeatherMarketLink.ticker, WeatherMarketLink.id)
        )
    )
    payload = {
        "generated_at": utc_now().isoformat(),
        "backup_kind": "LOGICAL_WEATHER_MARKET_LINK_ROWS",
        "purpose": "Backup exact local rows touched by Phase 3AZ-R12 missing-link apply.",
        "candidate_tickers": tickers,
        "candidate_rows": candidates,
        "markets": [_market_backup_row(market) for market in markets],
        "weather_market_links": [_weather_link_backup_row(link) for link in existing_links],
    }
    backup_text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    try:
        backup_path.write_text(backup_text, encoding="utf-8")
        return backup_path
    except PermissionError:
        fallback_path = output_dir / backup_name
        payload["backup_storage_warning"] = (
            f"Could not write backup under {backup_dir}; wrote fallback backup "
            f"inside the writable output directory instead."
        )
        fallback_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return fallback_path


def _market_backup_row(market: Market) -> dict[str, Any]:
    return {
        "ticker": market.ticker,
        "event_ticker": market.event_ticker,
        "series_ticker": market.series_ticker,
        "title": market.title,
        "subtitle": market.subtitle,
        "market_type": market.market_type,
        "status": market.status,
        "result": market.result,
        "open_time": _iso(market.open_time),
        "close_time": _iso(market.close_time),
        "expected_expiration_time": _iso(market.expected_expiration_time),
        "expiration_time": _iso(market.expiration_time),
        "settlement_ts": _iso(market.settlement_ts),
        "rules_primary": market.rules_primary,
        "rules_secondary": market.rules_secondary,
        "raw_json": decode_json(market.raw_json),
        "first_seen_at": _iso(market.first_seen_at),
        "last_seen_at": _iso(market.last_seen_at),
    }


def _weather_link_backup_row(link: WeatherMarketLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "ticker": link.ticker,
        "location_key": link.location_key,
        "detected_at": _iso(link.detected_at),
        "weather_metric": link.weather_metric,
        "target_operator": link.target_operator,
        "target_value": link.target_value,
        "target_time": _iso(link.target_time),
        "confidence": link.confidence,
        "reason": link.reason,
        "raw_json": decode_json(link.raw_json),
    }


def _same_decimal(left: Any, right: Any) -> bool:
    return to_decimal(left) == to_decimal(right)


def _writer_blocks_apply(writer: dict[str, Any]) -> bool:
    if bool(writer.get("safe_to_start_write", True)):
        return False
    current_writer = writer.get("current_writer") or {}
    if isinstance(current_writer, dict) and bool(current_writer.get("current_process")):
        return False
    return True


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_utc(value: Any):
    return parse_datetime(value)


def _iso(value: Any) -> str | None:
    parsed = _as_utc(value)
    return parsed.isoformat() if parsed is not None else None


def _session_db_url(session: Session) -> str | None:
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    return str(url) if url is not None else None


def _timestamp_for_path() -> str:
    return utc_now().strftime("%Y%m%d_%H%M%S")
