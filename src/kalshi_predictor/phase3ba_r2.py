from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import INACTIVE_MARKET_STATUSES
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    Forecast,
    Market,
    MarketOpportunity,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PaperPnl,
    WeatherMarketLink,
)
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.opportunities.reports import generate_opportunities_report
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE3BA_R2_VERSION = "phase3ba_r2_weather_ranking_activation_v1"
PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_EXCHANGE_WRITES"
MODEL_NAME = "weather_v2"
DEFAULT_OPPORTUNITY_OUTPUT = Path("reports/weather_opportunities.md")


@dataclass(frozen=True)
class Phase3BAR2ArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    markdown_path: Path
    rows_csv_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3ba_r2_weather_ranking_activation_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r2"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = 100,
    current_window_lookback_hours: int = 3,
    opportunity_output: Path = DEFAULT_OPPORTUNITY_OUTPUT,
) -> Phase3BAR2ArtifactSet:
    payload = build_phase3ba_r2_weather_ranking_activation(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        limit=limit,
        current_window_lookback_hours=current_window_lookback_hours,
        opportunity_output=opportunity_output,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "weather_ranking_activation.json"
    markdown_path = output_dir / "weather_ranking_activation.md"
    rows_csv_path = output_dir / "weather_opportunity_rows.csv"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_rows_csv(rows_csv_path, payload["weather_rows"])
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            json_path,
            markdown_path,
            rows_csv_path,
            next_actions_path,
        ],
    )
    return Phase3BAR2ArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        markdown_path=markdown_path,
        rows_csv_path=rows_csv_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_r2_weather_ranking_activation(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r2"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = 100,
    current_window_lookback_hours: int = 3,
    opportunity_output: Path = DEFAULT_OPPORTUNITY_OUTPUT,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    generated_at = utc_now()
    writer = _monitor_writer(resolved)
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=generated_at.isoformat(),
        command_args=command_args or [],
    )
    if writer.get("current_writer_pid") or not bool(writer.get("safe_to_start_write", True)):
        return {
            **metadata,
            "phase": "3BA-R2",
            "phase_version": PHASE3BA_R2_VERSION,
            "mode": "PAPER_ONLY_WEATHER_RANKING_ACTIVATION",
            "status": "BLOCKED_BY_ACTIVE_WRITER",
            "output_dir": str(output_dir),
            "reports_dir": str(reports_dir),
            "parameters": _parameters(
                limit=limit,
                current_window_lookback_hours=current_window_lookback_hours,
                opportunity_output=opportunity_output,
            ),
            "active_db_writer_status": writer,
            "before_summary": _empty_weather_summary(writer=writer),
            "after_summary": _empty_weather_summary(writer=writer),
            "opportunity_scan": {
                "ran": False,
                "status": "SKIPPED_ACTIVE_WRITER",
                "registered_command": _registered_weather_command(opportunity_output, limit=limit),
            },
            "weather_rows": [],
            "acceptance": _acceptance(
                status="BLOCKED_BY_ACTIVE_WRITER",
                before_summary=_empty_weather_summary(writer=writer),
                after_summary=_empty_weather_summary(writer=writer),
                opportunity_report_generated=False,
            ),
            "next_action": _next_action(
                status="BLOCKED_BY_ACTIVE_WRITER",
                after_summary=_empty_weather_summary(writer=writer),
            ),
            "operator_guardrails": _operator_guardrails(),
        }

    current_since = generated_at - timedelta(hours=max(current_window_lookback_hours, 0))
    before_rows = _current_weather_rows(
        session,
        current_since=current_since,
        model_name=MODEL_NAME,
        limit=max(limit * 3, limit, 1),
    )
    before_summary = _summary(before_rows, writer=writer)
    eligible_tickers = sorted({row["ticker"] for row in before_rows})
    opportunity_scan = _run_weather_opportunity_path(
        session,
        settings=resolved,
        ticker_scope=eligible_tickers,
        limit=limit,
        output_path=opportunity_output,
    )
    session.flush()
    after_rows = _current_weather_rows(
        session,
        current_since=current_since,
        model_name=MODEL_NAME,
        limit=max(limit * 3, limit, 1),
    )
    after_rows = _attach_before_ranking_state(before_rows=before_rows, after_rows=after_rows)
    after_summary = _summary(after_rows, writer=writer)
    status = _status(before_summary=before_summary, after_summary=after_summary)
    opportunity_report_generated = Path(opportunity_output).exists()
    return {
        **metadata,
        "phase": "3BA-R2",
        "phase_version": PHASE3BA_R2_VERSION,
        "mode": "PAPER_ONLY_WEATHER_RANKING_ACTIVATION",
        "status": status,
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "parameters": _parameters(
            limit=limit,
            current_window_lookback_hours=current_window_lookback_hours,
            opportunity_output=opportunity_output,
        ),
        "active_db_writer_status": writer,
        "before_summary": before_summary,
        "after_summary": after_summary,
        "opportunity_scan": opportunity_scan,
        "weather_rows": after_rows,
        "acceptance": _acceptance(
            status=status,
            before_summary=before_summary,
            after_summary=after_summary,
            opportunity_report_generated=opportunity_report_generated,
        ),
        "next_action": _next_action(status=status, after_summary=after_summary),
        "operator_guardrails": _operator_guardrails(),
    }


def _run_weather_opportunity_path(
    session: Session,
    *,
    settings: Settings,
    ticker_scope: list[str],
    limit: int,
    output_path: Path,
) -> dict[str, Any]:
    registered_command = _registered_weather_command(output_path, limit=limit)
    if not ticker_scope:
        return {
            "ran": False,
            "status": "SKIPPED_NO_CURRENT_WEATHER_LINKS",
            "registered_command": registered_command,
            "current_ticker_scope_count": 0,
            "expired_weather_markets_excluded": True,
        }
    report_path, summary = generate_opportunities_report(
        session,
        model_name=MODEL_NAME,
        limit=limit,
        output_path=output_path,
        settings=settings,
        ticker_scope=set(ticker_scope),
        scan_mode="CURRENT_WEATHER_RANKING_ACTIVATION",
    )
    return {
        "ran": True,
        "status": "COMPLETED",
        "registered_command": registered_command,
        "called_registered_scanner": "generate_opportunities_report",
        "current_ticker_scope_count": len(ticker_scope),
        "expired_weather_markets_excluded": True,
        "report_path": str(report_path),
        "markets_scanned": summary.markets_scanned,
        "rankings_inserted": summary.rankings_inserted,
        "opportunities_detected": summary.opportunities_detected,
        "historical_rows_excluded": summary.historical_rows_excluded,
        "first_hard_blocker": summary.first_hard_blocker,
        "top_opportunity_ticker": summary.top_opportunity_ticker,
        "top_opportunity_score": str(summary.top_opportunity_score)
        if summary.top_opportunity_score is not None
        else None,
    }


def _current_weather_rows(
    session: Session,
    *,
    current_since: Any,
    model_name: str,
    limit: int,
) -> list[dict[str, Any]]:
    links = _current_weather_links(session, current_since=current_since, limit=limit)
    settings = learning_paper_settings(get_settings())
    rows = []
    for link in links:
        market = session.get(Market, link.ticker)
        snapshot = _latest_snapshot(session, link.ticker)
        forecast = _latest_forecast(session, link.ticker, model_name=model_name)
        ranking = _latest_ranking(session, link.ticker, model_name=model_name)
        opportunity = _latest_opportunity(session, link.ticker, model_name=model_name)
        snapshot_at = snapshot.captured_at if snapshot is not None else None
        forecast_at = forecast.forecasted_at if forecast is not None else None
        ranking_at = ranking.ranked_at if ranking is not None else None
        has_current_forecast = bool(
            forecast_at is not None and (snapshot_at is None or forecast_at >= snapshot_at)
        )
        has_current_ranking = bool(
            ranking_at is not None and forecast_at is not None and ranking_at >= forecast_at
        )
        row = _row_payload(
            link=link,
            market=market,
            snapshot=snapshot,
            forecast=forecast,
            ranking=ranking if has_current_ranking else None,
            opportunity=opportunity,
            has_current_forecast=has_current_forecast,
            has_current_ranking=has_current_ranking,
            settings=settings,
        )
        rows.append(row)
    rows.sort(key=lambda row: (row["target_time"] or "", row["ticker"]), reverse=True)
    return rows


def _current_weather_links(
    session: Session,
    *,
    current_since: Any,
    limit: int,
    tickers: list[str] | tuple[str, ...] | None = None,
) -> list[WeatherMarketLink]:
    ticker_scope = list(
        dict.fromkeys(
            str(ticker).strip() for ticker in (tickers or ()) if str(ticker).strip()
        )
    )
    if tickers is not None and not ticker_scope:
        return []
    filters = [
        WeatherMarketLink.target_time.is_not(None),
        WeatherMarketLink.target_time >= current_since,
    ]
    if tickers is not None:
        filters.append(WeatherMarketLink.ticker.in_(ticker_scope))
    statement = (
        select(WeatherMarketLink)
        .where(*filters)
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
        if _market_is_inactive(session, link.ticker):
            continue
        latest.setdefault(link.ticker, link)
        if len(latest) >= limit:
            break
    return list(latest.values())


def _row_payload(
    *,
    link: WeatherMarketLink,
    market: Market | None,
    snapshot: MarketSnapshot | None,
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    opportunity: MarketOpportunity | None,
    has_current_forecast: bool,
    has_current_ranking: bool,
    settings: Settings,
) -> dict[str, Any]:
    raw_market = decode_json(market.raw_json) if market is not None else {}
    row = {
        "ticker": link.ticker,
        "location_key": link.location_key,
        "target_time": _iso(link.target_time),
        "link_detected_at": _iso(link.detected_at),
        "market_status": market.status if market is not None else None,
        "has_snapshot": snapshot is not None,
        "has_current_forecast": has_current_forecast,
        "has_current_ranking": has_current_ranking,
        "had_current_ranking_before": None,
        "latest_snapshot_at": _iso(snapshot.captured_at if snapshot is not None else None),
        "latest_forecast_at": _iso(forecast.forecasted_at if forecast is not None else None),
        "latest_ranking_at": _iso(ranking.ranked_at if ranking is not None else None),
        "has_opportunity": opportunity is not None,
        "opportunity_detected_at": _iso(
            opportunity.detected_at if opportunity is not None else None
        ),
        "best_side": ranking.best_side if ranking is not None else None,
        "best_price": ranking.best_price if ranking is not None else None,
        "estimated_edge": ranking.estimated_edge if ranking is not None else None,
        "opportunity_score": ranking.opportunity_score if ranking is not None else None,
        "spread": ranking.spread if ranking is not None else None,
        "liquidity": ranking.liquidity if ranking is not None else None,
        "liquidity_score": ranking.liquidity_score if ranking is not None else None,
        "time_to_close_minutes": ranking.time_to_close_minutes if ranking is not None else None,
        "model_confidence_score": (
            ranking.model_confidence_score if ranking is not None else None
        ),
        "ranking_reason": ranking.reason if ranking is not None else None,
        "settlement_terms_known": _settlement_terms_known(raw_market, ranking),
    }
    row["first_hard_blocker"] = _first_weather_blocker(row, settings=settings)
    return row


def _first_weather_blocker(row: dict[str, Any], *, settings: Settings) -> str:
    if not row["has_snapshot"]:
        return "SNAPSHOT_MISSING"
    if not row["has_current_forecast"]:
        return "FORECAST_MISSING"
    if not row["has_current_ranking"]:
        return "RANKING_MISSING"
    if row.get("best_side") is None or row.get("best_price") is None:
        return "BOOK_MISSING"
    edge = to_decimal(row.get("estimated_edge")) or Decimal("0")
    if edge <= 0:
        return "EV_NOT_POSITIVE"
    liquidity = to_decimal(row.get("liquidity")) or Decimal("0")
    if liquidity < settings.opportunity_min_liquidity:
        return "LIQUIDITY_TOO_LOW"
    spread = to_decimal(row.get("spread"))
    if spread is not None and spread > settings.opportunity_max_spread:
        return "SPREAD_TOO_WIDE"
    if not row.get("settlement_terms_known"):
        return "SETTLEMENT_TERMS_UNKNOWN"
    score = to_decimal(row.get("opportunity_score")) or Decimal("0")
    if edge < settings.opportunity_min_edge or score < settings.opportunity_min_score:
        return "RISK_NOT_ELIGIBLE"
    time_to_close = to_decimal(row.get("time_to_close_minutes"))
    if (
        time_to_close is not None
        and time_to_close < settings.opportunity_min_time_to_close_minutes
    ):
        return "RISK_NOT_ELIGIBLE"
    return "PAPER_GATE_READY"


def _settlement_terms_known(raw_market: dict[str, Any], ranking: MarketRanking | None) -> bool:
    if ranking is not None and ranking.time_to_close_minutes is not None:
        return True
    for key in ("close_time", "expected_expiration_time", "expiration_time", "settlement_sources"):
        if raw_market.get(key):
            return True
    return False


def _attach_before_ranking_state(
    *,
    before_rows: list[dict[str, Any]],
    after_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    before_by_ticker = {row["ticker"]: row for row in before_rows}
    for row in after_rows:
        before = before_by_ticker.get(row["ticker"]) or {}
        row["had_current_ranking_before"] = bool(before.get("has_current_ranking"))
    return after_rows


def _summary(rows: list[dict[str, Any]], *, writer: dict[str, Any]) -> dict[str, Any]:
    blockers: dict[str, int] = {}
    for row in rows:
        blocker = str(row.get("first_hard_blocker") or "UNKNOWN")
        blockers[blocker] = blockers.get(blocker, 0) + 1
    return {
        "current_weather_links": len(rows),
        "links_with_snapshots": sum(1 for row in rows if row["has_snapshot"]),
        "links_with_current_weather_forecasts": sum(
            1 for row in rows if row["has_current_forecast"]
        ),
        "links_with_current_weather_rankings": sum(
            1 for row in rows if row["has_current_ranking"]
        ),
        "snapshot_gap_rows": sum(1 for row in rows if not row["has_snapshot"]),
        "forecast_gap_rows": sum(
            1 for row in rows if row["has_snapshot"] and not row["has_current_forecast"]
        ),
        "ranking_gap_rows": sum(
            1
            for row in rows
            if row["has_current_forecast"] and not row["has_current_ranking"]
        ),
        "paper_gate_ready_rows": blockers.get("PAPER_GATE_READY", 0),
        "first_hard_blocker_counts": blockers,
        "db_writer_safe_to_start": bool(writer.get("safe_to_start_write", True)),
        "active_writer_pid": writer.get("current_writer_pid"),
    }


def _empty_weather_summary(*, writer: dict[str, Any]) -> dict[str, Any]:
    return {
        "current_weather_links": 0,
        "links_with_snapshots": 0,
        "links_with_current_weather_forecasts": 0,
        "links_with_current_weather_rankings": 0,
        "snapshot_gap_rows": 0,
        "forecast_gap_rows": 0,
        "ranking_gap_rows": 0,
        "paper_gate_ready_rows": 0,
        "first_hard_blocker_counts": {},
        "db_writer_safe_to_start": bool(writer.get("safe_to_start_write", False)),
        "active_writer_pid": writer.get("current_writer_pid"),
    }


def _status(*, before_summary: dict[str, Any], after_summary: dict[str, Any]) -> str:
    if after_summary["current_weather_links"] == 0:
        return "NO_CURRENT_WEATHER_LINKS"
    if after_summary["links_with_current_weather_rankings"] > before_summary[
        "links_with_current_weather_rankings"
    ]:
        return "WEATHER_RANKINGS_ACTIVATED"
    if after_summary["ranking_gap_rows"] < before_summary["ranking_gap_rows"]:
        return "WEATHER_RANKING_GAP_REDUCED"
    if after_summary["ranking_gap_rows"] == 0:
        return "WEATHER_RANKING_GATE_CLOSED"
    return "WEATHER_RANKING_GAP_EXPLAINED"


def _acceptance(
    *,
    status: str,
    before_summary: dict[str, Any],
    after_summary: dict[str, Any],
    opportunity_report_generated: bool,
) -> dict[str, Any]:
    return {
        "weather_ranking_gap_reduced_or_explained": status
        in {
            "WEATHER_RANKINGS_ACTIVATED",
            "WEATHER_RANKING_GAP_REDUCED",
            "WEATHER_RANKING_GATE_CLOSED",
            "WEATHER_RANKING_GAP_EXPLAINED",
            "NO_CURRENT_WEATHER_LINKS",
            "BLOCKED_BY_ACTIVE_WRITER",
        },
        "weather_opportunity_report_generated": opportunity_report_generated,
        "current_snapshot_forecast_rows_ranked_or_blocked": (
            after_summary["ranking_gap_rows"] == 0
            or bool(after_summary["first_hard_blocker_counts"])
        ),
        "rankings_before": before_summary["links_with_current_weather_rankings"],
        "rankings_after": after_summary["links_with_current_weather_rankings"],
        "no_live_or_demo_exchange_writes": True,
        "no_paper_trades_created": True,
        "next_actions_says_whether_to_run_paper_gate": True,
    }


def _next_action(*, status: str, after_summary: dict[str, Any]) -> dict[str, Any]:
    if status == "BLOCKED_BY_ACTIVE_WRITER":
        return {
            "stage": "WAIT_FOR_WRITER_CLEAR",
            "command": "kalshi-bot db-writer-monitor --json",
            "reason": "Weather ranking activation is write-capable and must wait.",
            "proceed_to_paper_gate_refresh": False,
        }
    if after_summary["paper_gate_ready_rows"] > 0:
        return {
            "stage": "RUN_PAPER_READY_GATE",
            "command": (
                "kalshi-bot phase3ap-paper-ready-unblock-report --output-dir "
                "reports/phase3ap --reports-dir reports"
            ),
            "reason": "Weather rows reached the paper gate readiness blocker.",
            "proceed_to_paper_gate_refresh": True,
        }
    if after_summary["links_with_current_weather_rankings"] > 0:
        return {
            "stage": "RUN_PAPER_READY_GATE",
            "command": (
                "kalshi-bot phase3ap-paper-ready-unblock-report --output-dir "
                "reports/phase3ap --reports-dir reports"
            ),
            "reason": "Weather rankings exist; refresh the canonical paper-ready gate.",
            "proceed_to_paper_gate_refresh": True,
        }
    if after_summary["ranking_gap_rows"] > 0:
        return {
            "stage": "RERUN_WEATHER_RANKING_AFTER_INPUT_GAPS",
            "command": (
                "kalshi-bot phase3ba-r2-weather-ranking-activation "
                "--output-dir reports/phase3ba_r2 --reports-dir reports"
            ),
            "reason": "Some current weather rows still lack rankings.",
            "proceed_to_paper_gate_refresh": False,
        }
    return {
        "stage": "REFRESH_CURRENT_WEATHER_WINDOW",
        "command": (
            "kalshi-bot phase3az-r13-weather-handoff-status --output-dir "
            "reports/phase3az_r13_weather --reports-dir reports"
        ),
        "reason": "No paper-gate-ready weather ranking exists yet.",
        "proceed_to_paper_gate_refresh": False,
    }


def _metadata(
    session: Session,
    *,
    settings: Settings,
    generated_at: str,
    command_args: list[str],
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    redacted_db_url = redact_database_url(db_url)
    return {
        "generated_at": generated_at,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": _git_dirty_status(),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "resolved_database_url": redacted_db_url,
        "database_fingerprint": _database_fingerprint(db_url),
        "database_location": describe_db_location(db_url),
        "migration_revision": _migration_revision(session),
        "timezone": getattr(settings, "timezone", None) or "UTC",
        "command_arguments": {
            "command": "kalshi-bot phase3ba-r2-weather-ranking-activation",
            "argv": command_args,
        },
        "data_watermark": _data_watermark(session),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "safety_flags": _safety_flags(),
    }


def _parameters(
    *,
    limit: int,
    current_window_lookback_hours: int,
    opportunity_output: Path,
) -> dict[str, Any]:
    return {
        "model_name": MODEL_NAME,
        "limit": limit,
        "current_window_lookback_hours": current_window_lookback_hours,
        "opportunity_output": str(opportunity_output),
        "registered_command": _registered_weather_command(opportunity_output, limit=limit),
        "current_ticker_scope_only": True,
    }


def _safety_flags() -> dict[str, Any]:
    return {
        "paper_only": True,
        "live_trading_enabled": False,
        "demo_exchange_writes_enabled": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "creates_live_or_demo_orders": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "creates_rankings": True,
        "creates_opportunity_rows": True,
        "fabricates_weather_data": False,
        "fabricates_forecasts": False,
        "fabricates_links": False,
        "uses_expired_weather_markets": False,
        "thresholds_lowered": False,
    }


def _operator_guardrails() -> list[str]:
    return [
        "Run only after db-writer-monitor reports safe_to_start_write=true.",
        "Weather opportunity ranking writes are local paper-only rankings/opportunity artifacts.",
        "Do not create paper trades in this phase.",
        "Do not submit, cancel, replace, or amend live/demo exchange orders.",
        "Do not rank expired weather markets; the scan is scoped to current weather links.",
        "Do not lower EV, confidence, liquidity, spread, settlement, or risk thresholds.",
    ]


def _registered_weather_command(output_path: Path, *, limit: int) -> str:
    return (
        "kalshi-bot find-opportunities --model-name weather_v2 "
        f"--limit {limit} --output {output_path}"
    )


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_forecast(session: Session, ticker: str, *, model_name: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == model_name)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _latest_ranking(session: Session, ticker: str, *, model_name: str) -> MarketRanking | None:
    return session.scalar(
        select(MarketRanking)
        .where(MarketRanking.ticker == ticker, MarketRanking.forecast_model == model_name)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(1)
    )


def _latest_opportunity(
    session: Session,
    ticker: str,
    *,
    model_name: str,
) -> MarketOpportunity | None:
    return session.scalar(
        select(MarketOpportunity)
        .where(MarketOpportunity.ticker == ticker, MarketOpportunity.model_name == model_name)
        .order_by(desc(MarketOpportunity.detected_at), desc(MarketOpportunity.id))
        .limit(1)
    )


def _market_is_inactive(session: Session, ticker: str) -> bool:
    status = session.scalar(select(Market.status).where(Market.ticker == ticker).limit(1))
    return bool(status and str(status).lower() in INACTIVE_MARKET_STATUSES)


def _monitor_writer(settings: Settings) -> dict[str, Any]:
    try:
        return db_writer_monitor(settings=settings)
    except Exception as exc:  # noqa: BLE001 - fail closed into report.
        return {
            "status": "UNKNOWN",
            "safe_to_start_write": False,
            "current_writer_pid": None,
            "current_writer_command": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _data_watermark(session: Session) -> dict[str, Any]:
    return {
        "latest_market_seen_at": _latest_iso(session, Market.last_seen_at),
        "latest_snapshot_at": _latest_iso(session, MarketSnapshot.captured_at),
        "latest_weather_v2_forecast_at": _latest_forecast_iso(session),
        "latest_weather_v2_ranking_at": _latest_ranking_iso(session),
        "latest_paper_order_at": _latest_iso(session, PaperOrder.created_at),
        "latest_paper_pnl_at": _latest_iso(session, PaperPnl.calculated_at),
    }


def _latest_iso(session: Session, column: Any) -> str | None:
    value = session.scalar(select(func.max(column)))
    return value.isoformat() if hasattr(value, "isoformat") else value


def _latest_forecast_iso(session: Session) -> str | None:
    value = session.scalar(
        select(func.max(Forecast.forecasted_at)).where(Forecast.model_name == MODEL_NAME)
    )
    return value.isoformat() if hasattr(value, "isoformat") else value


def _latest_ranking_iso(session: Session) -> str | None:
    value = session.scalar(
        select(func.max(MarketRanking.ranked_at)).where(
            MarketRanking.forecast_model == MODEL_NAME
        )
    )
    return value.isoformat() if hasattr(value, "isoformat") else value


def _database_fingerprint(db_url: str) -> dict[str, Any]:
    redacted = redact_database_url(db_url)
    sqlite_path = sqlite_path_from_url(db_url)
    if sqlite_path is None:
        return {
            "kind": "non_sqlite",
            "database_url_hash": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
        }
    if str(sqlite_path) == ":memory:":
        return {"kind": "sqlite_memory", "path": ":memory:"}
    path = sqlite_path.expanduser().resolve()
    if not path.exists():
        return {"kind": "missing_sqlite_file", "path": str(path)}
    stat = path.stat()
    payload = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    return {
        "kind": "sqlite_file_stat",
        **payload,
        "fingerprint": hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _migration_revision(session: Session) -> str | None:
    try:
        return session.execute(text("select version_num from alembic_version limit 1")).scalar()
    except Exception:
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "UNKNOWN"


def _git_dirty_status() -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    if result.returncode != 0:
        return "UNKNOWN"
    return "dirty" if result.stdout.strip() else "clean"


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R2 Weather Ranking Activation")
    after = payload["after_summary"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{payload['status']}`",
            f"- Current weather links: `{after['current_weather_links']}`",
            f"- Links with snapshots: `{after['links_with_snapshots']}`",
            f"- Links with forecasts: `{after['links_with_current_weather_forecasts']}`",
            f"- Links with rankings: `{after['links_with_current_weather_rankings']}`",
            f"- Ranking gap rows: `{after['ranking_gap_rows']}`",
            f"- Paper-gate-ready rows: `{after['paper_gate_ready_rows']}`",
            "",
            "## Opportunity Scan",
            "",
        ]
    )
    for key, value in payload["opportunity_scan"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Next Action",
            "",
            f"- Stage: `{payload['next_action']['stage']}`",
            f"- Command: `{payload['next_action']['command']}`",
            f"- Proceed to paper gate refresh: "
            f"`{payload['next_action']['proceed_to_paper_gate_refresh']}`",
            f"- Reason: {payload['next_action']['reason']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R2 Weather Ranking Activation Detail")
    lines.extend(
        [
            "",
            "## Before Summary",
            "",
        ]
    )
    for key, value in payload["before_summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## After Summary", ""])
    for key, value in payload["after_summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Weather Rows",
            "",
            "| Ticker | Snapshot | Forecast | Ranking | Blocker | Edge | Score |",
            "| --- | --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    if not payload["weather_rows"]:
        lines.append("| none |  |  |  | NO_CURRENT_WEATHER_LINKS |  |  |")
    for row in payload["weather_rows"]:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{row['has_snapshot']} | "
            f"{row['has_current_forecast']} | "
            f"{row['has_current_ranking']} | "
            f"{row['first_hard_blocker']} | "
            f"{row.get('estimated_edge') or ''} | "
            f"{row.get('opportunity_score') or ''} |"
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R2 Next Actions")
    next_action = payload["next_action"]
    lines.extend(
        [
            "",
            "## Exact Next Operator Command",
            "",
            f"```bash\n{next_action['command']}\n```",
            "",
            f"- Stage: `{next_action['stage']}`",
            f"- Proceed to paper gate refresh: `{next_action['proceed_to_paper_gate_refresh']}`",
            f"- Reason: {next_action['reason']}",
            "",
            "## Guardrails",
            "",
        ]
    )
    for guardrail in payload["operator_guardrails"]:
        lines.append(f"- {guardrail}")
    return "\n".join(lines) + "\n"


def _metadata_lines(payload: dict[str, Any], *, title: str) -> list[str]:
    return [
        title,
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Git commit: `{payload['git_commit']}`",
        f"- DB fingerprint: `{json.dumps(payload['database_fingerprint'], sort_keys=True)}`",
        f"- Data watermark: `{json.dumps(payload['data_watermark'], sort_keys=True)}`",
        f"- Command args: `{json.dumps(payload['command_arguments'], sort_keys=True)}`",
        f"- Safety flags: `{json.dumps(payload['safety_flags'], sort_keys=True)}`",
        f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
        "- Order submission/cancel/replace: "
        f"`{payload['order_submission'] or payload['order_cancel_replace']}`",
        f"- Paper trade creation: `{payload['paper_trade_creation']}`",
    ]


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker",
        "location_key",
        "target_time",
        "has_snapshot",
        "has_current_forecast",
        "had_current_ranking_before",
        "has_current_ranking",
        "first_hard_blocker",
        "latest_snapshot_at",
        "latest_forecast_at",
        "latest_ranking_at",
        "best_side",
        "best_price",
        "estimated_edge",
        "opportunity_score",
        "spread",
        "liquidity",
        "liquidity_score",
        "time_to_close_minutes",
        "model_confidence_score",
        "ranking_reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
