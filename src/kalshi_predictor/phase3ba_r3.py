from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PaperPnl,
    PositionSizingDecisionLog,
    WeatherFeature,
    WeatherForecast,
    WeatherMarketLink,
)
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.opportunities.market_identity import verify_market_identity
from kalshi_predictor.opportunities.window_eligibility import current_market_window_status
from kalshi_predictor.paper.models import BUY_NO
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ap import (
    MIN_EXECUTABLE_LIQUIDITY_SCORE,
    QUOTE_STALE_AFTER_MINUTES,
    RAW_EV_COST_BUFFER,
    _forecast_id_from_ranking,
    _paper_order_keys,
    _phase3ap_book_probe,
    _settlement_entry_check,
)
from kalshi_predictor.phase3ba_r2 import (
    _latest_forecast,
    _latest_ranking,
    _latest_snapshot,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now
from kalshi_predictor.weather.repository import normalize_location_key

PHASE3BA_R3_VERSION = "phase3ba_r3_weather_paper_gate_v1"
MODEL_NAME = "weather_v2"

WEATHER_PAPER_BLOCKERS = (
    "MARKET_WINDOW_INELIGIBLE",
    "RAIN_HORIZON_INCOMPLETE",
    "LINK_UNVERIFIED",
    "SNAPSHOT_MISSING",
    "SOURCE_MISSING",
    "SNAPSHOT_STALE",
    "FORECAST_MISSING",
    "RANKING_MISSING",
    "EV_NOT_POSITIVE",
    "EXECUTABLE_EV_NOT_POSITIVE",
    "BOOK_MISSING",
    "LIQUIDITY_TOO_LOW",
    "SPREAD_TOO_WIDE",
    "SETTLEMENT_TERMS_UNKNOWN",
    "RISK_NOT_ELIGIBLE",
    "PHASE_3M_ZERO_SIZE",
    "PHASE_3N_RISK_BLOCK",
    "PAPER_READY",
)


@dataclass(frozen=True)
class Phase3BAR3ArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    markdown_path: Path
    rows_csv_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3ba_r3_weather_paper_gate_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r3"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = 500,
    current_window_lookback_hours: int = 3,
    match_tolerance_hours: int = 3,
    tickers: list[str] | tuple[str, ...] | None = None,
    deadline_seconds: int = 0,
    batch_size: int = 4,
) -> Phase3BAR3ArtifactSet:
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "weather_paper_gate_progress.json"
    identity_cache_path = output_dir / "market_identity_cache.json"
    payload = build_phase3ba_r3_weather_paper_gate(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        limit=limit,
        current_window_lookback_hours=current_window_lookback_hours,
        match_tolerance_hours=match_tolerance_hours,
        tickers=tickers,
        deadline_seconds=deadline_seconds,
        batch_size=batch_size,
        progress_path=progress_path,
        identity_cache_path=identity_cache_path,
    )
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "weather_paper_gate.json"
    markdown_path = output_dir / "weather_paper_gate.md"
    rows_csv_path = output_dir / "weather_paper_gate_rows.csv"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json_atomic(json_path, payload)
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
    return Phase3BAR3ArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        markdown_path=markdown_path,
        rows_csv_path=rows_csv_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_r3_weather_paper_gate(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r3"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = 500,
    current_window_lookback_hours: int = 3,
    match_tolerance_hours: int = 3,
    tickers: list[str] | tuple[str, ...] | None = None,
    deadline_seconds: int = 0,
    batch_size: int = 4,
    progress_path: Path | None = None,
    identity_cache_path: Path | None = None,
) -> dict[str, Any]:
    base_settings = settings or get_settings()
    resolved = learning_paper_settings(base_settings)
    now = utc_now()
    metadata = _metadata(
        session,
        settings=base_settings,
        generated_at=now.isoformat(),
        command_args=command_args or [],
        include_data_watermark=False,
    )
    current_since = now - timedelta(hours=max(current_window_lookback_hours, 0))
    candidate_links = _current_valid_weather_links(
        session,
        current_since=current_since,
        limit=limit,
        tickers=tickers,
    )
    links = _valid_weather_links(candidate_links)
    unknown_location_links_rejected = _unknown_current_link_count(
        session, current_since=current_since, tickers=tickers
    )
    selected_tickers = sorted({link.ticker for link in links})
    sizing = _latest_by_ticker(
        session, PositionSizingDecisionLog, selected_tickers, "decision_timestamp"
    )
    risk = _latest_by_ticker(
        session, AdvancedRiskDecisionLog, selected_tickers, "decision_timestamp"
    )
    paper_orders = _paper_order_keys(session, selected_tickers)
    weather_features = _weather_features_for_links(
        session,
        links,
        settings=resolved,
        match_tolerance_hours=match_tolerance_hours,
    )
    source_forecasts = _weather_source_forecasts_for_links(
        session,
        links,
        match_tolerance_hours=match_tolerance_hours,
    )
    started = time.monotonic()
    cache = _read_identity_cache(identity_cache_path)
    rows: list[dict[str, Any]] = []
    deadline_reached = False
    for offset in range(0, len(links), max(1, batch_size)):
        if deadline_seconds and time.monotonic() - started >= deadline_seconds:
            deadline_reached = True
            break
        for link in links[offset : offset + max(1, batch_size)]:
            market = session.get(Market, link.ticker)
            cache_key = _identity_cache_key(market)
            identity_payload = cache.get(cache_key)
            if identity_payload is None:
                ranking = _latest_ranking(session, link.ticker, model_name=MODEL_NAME)
                identity_payload = verify_market_identity(
                    session,
                    ticker=link.ticker,
                    ranking=ranking,
                    market=market,
                    settings=resolved,
                ).as_dict()
                cache[cache_key] = identity_payload
            rows.append(
                _weather_paper_gate_row(
                    session,
                    link,
                    settings=resolved,
                    now=now,
                    sizing=sizing.get(link.ticker),
                    risk=risk.get(link.ticker),
                    paper_orders=paper_orders,
                    feature=weather_features.get(link.ticker),
                    source_forecast=source_forecasts.get(link.ticker),
                    identity_payload=identity_payload,
                )
            )
        _write_gate_progress(
            progress_path,
            generated_at=now.isoformat(),
            rows=rows,
            total=len(links),
            deadline_reached=False,
        )
        _write_identity_cache(identity_cache_path, cache)
    if len(rows) < len(links):
        deadline_reached = True
    _write_gate_progress(
        progress_path,
        generated_at=now.isoformat(),
        rows=rows,
        total=len(links),
        deadline_reached=deadline_reached,
    )
    summary = _summary(rows)
    status = "PARTIAL_DEADLINE_REACHED" if deadline_reached else _status(summary)
    return {
        **metadata,
        "phase": "3BA-R3",
        "phase_version": PHASE3BA_R3_VERSION,
        "mode": "PAPER_READ_ONLY_WEATHER_PAPER_GATE",
        "status": status,
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "parameters": {
            "limit": limit,
            "current_window_lookback_hours": current_window_lookback_hours,
            "match_tolerance_hours": match_tolerance_hours,
            "ticker_scope_count": len(tickers or ()),
            "active_ticker_scope": tickers is not None,
            "candidate_links": len(candidate_links),
            "unknown_location_links_rejected": unknown_location_links_rejected,
            "deadline_seconds": deadline_seconds,
            "batch_size": batch_size,
            "model_name": MODEL_NAME,
            "quote_stale_after_minutes": str(QUOTE_STALE_AFTER_MINUTES),
            "min_executable_liquidity_score": str(MIN_EXECUTABLE_LIQUIDITY_SCORE),
        },
        "stale_3ap_truth_ignored": True,
        "weather_paper_funnel": list(_funnel_steps()),
        "weather_blocker_order": list(WEATHER_PAPER_BLOCKERS),
        "summary": summary,
        "weather_rows": rows,
        "progress": {
            "rows_processed": len(rows),
            "rows_total": len(links),
            "complete": len(rows) == len(links),
            "deadline_reached": deadline_reached,
        },
        "acceptance": _acceptance(summary),
        "next_action": _next_action(status=status, summary=summary),
        "operator_guardrails": _operator_guardrails(),
    }


def _weather_paper_gate_row(
    session: Session,
    link: WeatherMarketLink,
    *,
    settings: Settings,
    now: Any,
    sizing: PositionSizingDecisionLog | None,
    risk: AdvancedRiskDecisionLog | None,
    paper_orders: set[tuple[str, str, int | None]],
    feature: WeatherFeature | None,
    source_forecast: WeatherForecast | None,
    identity_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market = session.get(Market, link.ticker)
    snapshot = _latest_snapshot(session, link.ticker)
    forecast = _latest_forecast(session, link.ticker, model_name=MODEL_NAME)
    ranking = _latest_ranking(session, link.ticker, model_name=MODEL_NAME)
    if identity_payload is None:
        identity_payload = verify_market_identity(
            session,
            ticker=link.ticker,
            ranking=ranking,
            market=market,
            settings=settings,
        ).as_dict()
    exact_identity_verified = (
        decode_json(link.raw_json).get("exact_market_identity_verified") is True
    )
    snapshot_age = _age_minutes(snapshot.captured_at, now) if snapshot is not None else None
    snapshot_fresh = bool(
        snapshot is not None
        and snapshot_age is not None
        and snapshot_age <= QUOTE_STALE_AFTER_MINUTES
    )
    feature_age = _weather_feature_age_hours(feature, now=now) if feature is not None else None
    source_age = (
        _age_hours(source_forecast.forecast_generated_at, now)
        if source_forecast is not None
        else None
    )
    feature_fresh = bool(
        feature is not None
        and feature_age is not None
        and feature_age <= settings.weather_v2_max_forecast_age_hours
    )
    source_forecast_fresh = bool(
        source_forecast is not None
        and source_age is not None
        and source_age <= settings.weather_v2_max_forecast_age_hours
    )
    has_current_forecast = bool(
        forecast is not None
        and snapshot is not None
        and forecast.forecasted_at is not None
        and snapshot.captured_at is not None
        and forecast.forecasted_at >= snapshot.captured_at
    )
    has_current_ranking = bool(
        ranking is not None
        and forecast is not None
        and ranking.ranked_at is not None
        and forecast.forecasted_at is not None
        and ranking.ranked_at >= forecast.forecasted_at
    )
    window = current_market_window_status(
        market,
        settings=settings,
        ranking=ranking,
        now=now,
    )
    raw_ev, executable_ev = _ev_values(ranking=ranking, forecast=forecast)
    book = _book_probe(
        ranking=ranking,
        market=market,
        identity={
            **identity_payload,
            "exact_market_identity_verified": exact_identity_verified,
        },
        snapshot=snapshot,
        snapshot_age=snapshot_age,
        settings=settings,
        window=window,
    )
    settlement = _settlement_entry_check(
        session,
        ticker=link.ticker,
        market=market,
        identity=identity_payload,
    )
    forecast_id = _forecast_id_from_ranking(ranking) if ranking is not None else None
    forecast_features = decode_json(forecast.feature_json) if forecast is not None else {}
    feature_alignment = forecast_features.get("weather_feature_alignment")
    duplicate = (link.ticker, MODEL_NAME, forecast_id) in paper_orders
    phase3s_proceed = bool(
        ranking is not None
        and (to_decimal(ranking.opportunity_score) or Decimal("0"))
        >= settings.opportunity_min_score
    )
    phase3m_contracts = int(getattr(sizing, "proposed_contracts", 0) or 0)
    phase3n_action = str(getattr(risk, "action", "") or "").upper()
    row = {
        "ticker": link.ticker,
        "location_key": link.location_key,
        "target_time": _iso(link.target_time),
        "link_detected_at": _iso(link.detected_at),
        "link_confidence": link.confidence,
        "market_status": getattr(market, "status", None),
        "market_title": getattr(market, "title", None),
        "verified_kalshi_url": bool(identity_payload.get("kalshi_url_verified"))
        or exact_identity_verified,
        "exact_market_identity_verified": exact_identity_verified,
        "kalshi_url": identity_payload.get("kalshi_url"),
        "kalshi_url_status": identity_payload.get("kalshi_url_status"),
        "source_lineage": identity_payload.get("source_lineage"),
        "current_window_eligible": bool(window.get("current_window_eligible")),
        "window_status": window.get("window_status"),
        "window_status_reason": window.get("window_status_reason"),
        "has_snapshot": snapshot is not None,
        "snapshot_fresh": snapshot_fresh,
        "snapshot_age_minutes": decimal_to_str(snapshot_age),
        "latest_snapshot_at": _iso(snapshot.captured_at if snapshot else None),
        "has_weather_source_forecast": source_forecast is not None,
        "weather_source_forecast_fresh": source_forecast_fresh,
        "weather_source_forecast_age_hours": decimal_to_str(source_age),
        "weather_source_forecast_at": _iso(
            source_forecast.forecast_generated_at if source_forecast else None
        ),
        "has_weather_feature": feature is not None,
        "weather_feature_fresh": feature_fresh,
        "weather_feature_age_hours": decimal_to_str(feature_age),
        "weather_feature_target_time": _iso(feature.target_time if feature else None),
        "has_forecast": forecast is not None,
        "weather_feature_alignment": feature_alignment,
        "terminal_weather_horizon_complete": (
            feature_alignment != "LATEST_RAIN_RISK_WITHIN_NOAA_HORIZON"
        ),
        "has_current_forecast": has_current_forecast,
        "latest_forecast_at": _iso(forecast.forecasted_at if forecast else None),
        "has_ranking": ranking is not None,
        "has_current_ranking": has_current_ranking,
        "latest_ranking_at": _iso(ranking.ranked_at if ranking else None),
        "best_side": getattr(ranking, "best_side", None),
        "best_price": getattr(ranking, "best_price", None),
        "forecast_probability": getattr(ranking, "forecast_probability", None)
        or (forecast.yes_probability if forecast is not None else None),
        "raw_ev": decimal_to_str(raw_ev),
        "executable_ev": decimal_to_str(executable_ev),
        "estimated_edge": getattr(ranking, "estimated_edge", None),
        "opportunity_score": getattr(ranking, "opportunity_score", None),
        "spread": getattr(ranking, "spread", None),
        "liquidity": getattr(ranking, "liquidity", None),
        "liquidity_score": getattr(ranking, "liquidity_score", None),
        "executable_book": bool(book.get("executable_book")),
        "best_yes_bid": book.get("best_yes_bid"),
        "best_yes_ask": book.get("best_yes_ask"),
        "best_no_bid": book.get("best_no_bid"),
        "best_no_ask": book.get("best_no_ask"),
        "best_yes_bid_depth": book.get("best_yes_bid_depth"),
        "best_no_bid_depth": book.get("best_no_bid_depth"),
        "book_reason": book.get("book_reason"),
        "no_book_reason": book.get("no_book_reason"),
        "visible_depth": book.get("visible_depth"),
        "depth_at_configured_limit": book.get("depth_at_configured_limit"),
        "derived_executable_buy_price": book.get("derived_executable_buy_price"),
        "ranked_buy_price": book.get("ranked_buy_price"),
        "buy_price_matches_ranking": bool(book.get("buy_price_matches_ranking")),
        "actual_buy_side": book.get("actual_buy_side"),
        "actual_buy_price_source": book.get("actual_buy_price_source"),
        "minimum_buy_side_size": book.get("minimum_buy_side_size"),
        "sufficient_buy_side_size": bool(book.get("sufficient_buy_side_size")),
        "settlement_terms_known": bool(settlement.get("settlement_terms_known")),
        "settlement_specific_reason": settlement.get("specific_reason_code"),
        "paper_entry_settlement_eligible": bool(settlement.get("paper_entry_settlement_eligible")),
        "phase3s_proceed": phase3s_proceed,
        "phase3m_nonzero_size": phase3m_contracts > 0,
        "phase3m_proposed_contracts": phase3m_contracts,
        "phase3n_approved": phase3n_action in {"ALLOW", "APPROVE", "PROCEED"},
        "phase3n_action": phase3n_action or None,
        "duplicate_existing_paper_order": duplicate,
        "ranking_id": getattr(ranking, "id", None),
        "forecast_id": getattr(forecast, "id", None),
        "weather_feature_id": getattr(feature, "id", None),
        "weather_source_forecast_id": getattr(source_forecast, "id", None),
    }
    row["first_blocker"] = _first_weather_paper_blocker(row)
    row["paper_ready"] = row["first_blocker"] == "PAPER_READY"
    row["entered_paper_gate"] = bool(
        row["current_window_eligible"]
        and row["verified_kalshi_url"]
        and row["snapshot_fresh"]
        and row["has_weather_feature"]
        and row["weather_feature_fresh"]
        and row["has_current_forecast"]
        and row["has_current_ranking"]
    )
    return row


def _identity_cache_key(market: Market | None) -> str:
    if market is None:
        return "missing"
    return f"{market.ticker}|{_iso(market.last_seen_at)}"


def _valid_weather_links(links: list[WeatherMarketLink]) -> list[WeatherMarketLink]:
    return [
        link for link in links if normalize_location_key(link.location_key) not in {"", "unknown"}
    ]


def _current_valid_weather_links(
    session: Session,
    *,
    current_since: Any,
    limit: int,
    tickers: list[str] | tuple[str, ...] | None = None,
) -> list[WeatherMarketLink]:
    """Select current, active, normalized links without per-ticker market queries."""
    ticker_scope = list(dict.fromkeys(str(value).strip() for value in tickers or () if value))
    if tickers is not None and not ticker_scope:
        return []
    filters = [
        WeatherMarketLink.target_time.is_not(None),
        WeatherMarketLink.target_time >= current_since,
        func.lower(func.trim(WeatherMarketLink.location_key)).not_in(("", "unknown")),
        func.lower(func.coalesce(Market.status, "")).in_(("active", "open")),
        or_(Market.close_time.is_(None), Market.close_time > utc_now()),
    ]
    if tickers is not None:
        filters.append(WeatherMarketLink.ticker.in_(ticker_scope))
    statement = (
        select(WeatherMarketLink)
        .join(Market, Market.ticker == WeatherMarketLink.ticker)
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
        latest.setdefault(link.ticker, link)
        if len(latest) >= limit:
            break
    return list(latest.values())


def _unknown_current_link_count(
    session: Session,
    *,
    current_since: Any,
    tickers: list[str] | tuple[str, ...] | None,
) -> int:
    filters = [
        WeatherMarketLink.target_time >= current_since,
        func.lower(func.trim(WeatherMarketLink.location_key)) == "unknown",
    ]
    if tickers is not None:
        ticker_scope = [str(value).strip() for value in tickers if str(value).strip()]
        if not ticker_scope:
            return 0
        filters.append(WeatherMarketLink.ticker.in_(ticker_scope))
    return int(
        session.scalar(select(func.count(func.distinct(WeatherMarketLink.ticker))).where(*filters))
        or 0
    )


def _read_identity_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def _write_identity_cache(path: Path | None, entries: dict[str, dict[str, Any]]) -> None:
    if path is not None:
        _write_json_atomic(path, {"generated_at": utc_now().isoformat(), "entries": entries})


def _write_gate_progress(
    path: Path | None,
    *,
    generated_at: str,
    rows: list[dict[str, Any]],
    total: int,
    deadline_reached: bool,
) -> None:
    if path is None:
        return
    _write_json_atomic(
        path,
        {
            "generated_at": generated_at,
            "status": (
                "PARTIAL_DEADLINE_REACHED"
                if deadline_reached
                else "COMPLETE"
                if len(rows) == total
                else "IN_PROGRESS"
            ),
            "rows_processed": len(rows),
            "rows_total": total,
            "complete": len(rows) == total,
            "weather_rows": rows,
        },
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _first_weather_paper_blocker(row: dict[str, Any]) -> str:
    if not row.get("current_window_eligible"):
        return "MARKET_WINDOW_INELIGIBLE"
    if not row.get("terminal_weather_horizon_complete", True):
        return "RAIN_HORIZON_INCOMPLETE"
    if not row.get("verified_kalshi_url"):
        return "LINK_UNVERIFIED"
    if not row.get("has_snapshot"):
        return "SNAPSHOT_MISSING"
    if not row.get("snapshot_fresh"):
        return "SNAPSHOT_STALE"
    if not row.get("has_weather_source_forecast") or not row.get("weather_source_forecast_fresh"):
        return "SOURCE_MISSING"
    if not row.get("has_weather_feature") or not row.get("weather_feature_fresh"):
        return "SOURCE_MISSING"
    if not row.get("has_current_forecast"):
        return "FORECAST_MISSING"
    if not row.get("has_current_ranking"):
        return "RANKING_MISSING"
    raw_ev = to_decimal(row.get("raw_ev"))
    if raw_ev is None or raw_ev <= 0:
        return "EV_NOT_POSITIVE"
    executable_ev = to_decimal(row.get("executable_ev"))
    if executable_ev is None or executable_ev <= 0:
        return "EXECUTABLE_EV_NOT_POSITIVE"
    if row.get("no_book_reason") in {
        "ZERO_VISIBLE_DEPTH",
        "INSUFFICIENT_DEPTH",
        "INSUFFICIENT_BUY_SIDE_SIZE",
    }:
        return "LIQUIDITY_TOO_LOW"
    if row.get("no_book_reason") == "WIDE_SPREAD":
        return "SPREAD_TOO_WIDE"
    if not row.get("executable_book"):
        return "BOOK_MISSING"
    spread = to_decimal(row.get("spread"))
    if spread is not None and spread > (to_decimal(row.get("max_spread")) or Decimal("1")):
        return "SPREAD_TOO_WIDE"
    if not row.get("settlement_terms_known") or not row.get("paper_entry_settlement_eligible"):
        return "SETTLEMENT_TERMS_UNKNOWN"
    if not row.get("phase3s_proceed"):
        return "RISK_NOT_ELIGIBLE"
    if not row.get("phase3m_nonzero_size"):
        return "PHASE_3M_ZERO_SIZE"
    if not row.get("phase3n_approved"):
        return "PHASE_3N_RISK_BLOCK"
    return "PAPER_READY"


def _ev_values(
    *,
    ranking: MarketRanking | None,
    forecast: Forecast | None,
) -> tuple[Decimal | None, Decimal | None]:
    if ranking is None:
        return None, None
    probability = to_decimal(forecast.yes_probability if forecast else ranking.forecast_probability)
    price = to_decimal(ranking.best_price)
    side = str(ranking.best_side or "")
    if probability is None or price is None:
        return None, None
    side_probability = Decimal("1") - probability if side == BUY_NO else probability
    raw_ev = side_probability - price
    spread = to_decimal(ranking.spread) or Decimal("0")
    return raw_ev, raw_ev - spread - RAW_EV_COST_BUFFER


def _book_probe(
    *,
    ranking: MarketRanking | None,
    market: Market | None,
    identity: dict[str, Any],
    snapshot: MarketSnapshot | None,
    snapshot_age: Decimal | None,
    settings: Settings,
    window: dict[str, Any],
) -> dict[str, Any]:
    if ranking is None:
        return _empty_book("RANKING_MISSING")
    book = _phase3ap_book_probe(
        ranking=ranking,
        market=market,
        identity=identity,
        snapshot=snapshot,
        side=str(ranking.best_side or ""),
        quote_age=snapshot_age,
        settings=settings,
        window=window,
    )
    has_comparable_buy_prices = bool(
        book.get("derived_executable_buy_price") is not None
        and book.get("ranked_buy_price") is not None
    )
    if has_comparable_buy_prices and not book.get("buy_price_matches_ranking"):
        book["executable_book"] = False
        book["no_book_reason"] = "BUY_PRICE_MISMATCH"
        book["book_reason"] = (
            "The ranking BUY price does not equal the executable ask derived from the "
            "opposite-side bid in the same snapshot."
        )
    elif (
        book.get("buy_price_matches_ranking")
        and not book.get("sufficient_buy_side_size")
        and book.get("no_book_reason") in {None, "INSUFFICIENT_DEPTH"}
    ):
        book["executable_book"] = False
        book["no_book_reason"] = "INSUFFICIENT_BUY_SIDE_SIZE"
        book["book_reason"] = (
            "The actual BUY-side price level has less visible size than the configured minimum."
        )
    return book


def _empty_book(reason: str) -> dict[str, Any]:
    return {
        "best_yes_bid": None,
        "best_yes_ask": None,
        "best_no_bid": None,
        "best_no_ask": None,
        "best_yes_bid_depth": None,
        "best_no_bid_depth": None,
        "derived_executable_buy_price": None,
        "ranked_buy_price": None,
        "buy_price_matches_ranking": False,
        "actual_buy_side": None,
        "actual_buy_price_source": None,
        "minimum_buy_side_size": None,
        "sufficient_buy_side_size": False,
        "visible_depth": None,
        "depth_at_configured_limit": None,
        "book_source": "not_checked",
        "book_freshness_state": "NOT_CHECKED",
        "executable_book": False,
        "no_book_reason": reason,
        "book_reason": reason,
    }


def _weather_features_for_links(
    session: Session,
    links: list[WeatherMarketLink],
    *,
    settings: Settings,
    match_tolerance_hours: int,
) -> dict[str, WeatherFeature]:
    candidates_by_location: dict[str, list[WeatherFeature]] = {}
    for location_key in dict.fromkeys(
        _effective_location_key(link.location_key, settings) for link in links
    ):
        candidates_by_location[location_key] = list(
            session.scalars(
                select(WeatherFeature)
                .where(WeatherFeature.location_key == location_key)
                .order_by(desc(WeatherFeature.generated_at), WeatherFeature.target_time)
                .limit(200)
            )
        )
    return {
        link.ticker: selected
        for link in links
        if link.target_time is not None
        and (
            selected := _nearest_target_time(
                candidates_by_location.get(
                    _effective_location_key(link.location_key, settings),
                    [],
                ),
                link.target_time,
                match_tolerance_hours,
            )
        )
        is not None
    }


def _weather_source_forecasts_for_links(
    session: Session,
    links: list[WeatherMarketLink],
    *,
    match_tolerance_hours: int,
) -> dict[str, WeatherForecast]:
    candidates_by_location: dict[str, list[WeatherForecast]] = {}
    for location_key in dict.fromkeys(normalize_location_key(link.location_key) for link in links):
        candidates_by_location[location_key] = list(
            session.scalars(
                select(WeatherForecast)
                .where(WeatherForecast.location_key == location_key)
                .order_by(
                    desc(WeatherForecast.forecast_generated_at),
                    WeatherForecast.forecast_time,
                )
                .limit(200)
            )
        )
    return {
        link.ticker: selected
        for link in links
        if link.target_time is not None
        and (
            selected := _nearest_target_time(
                candidates_by_location.get(normalize_location_key(link.location_key), []),
                link.target_time,
                match_tolerance_hours,
            )
        )
        is not None
    }


def _nearest_target_time(
    candidates: list[Any],
    target_time: Any,
    match_tolerance_hours: int,
) -> Any | None:
    target = _as_utc(target_time)
    if target is None or not candidates:
        return None

    def distance_hours(candidate: Any) -> Decimal:
        candidate_time = _as_utc(
            getattr(candidate, "target_time", None) or getattr(candidate, "forecast_time", None)
        )
        if candidate_time is None:
            return Decimal("999999")
        return Decimal(str(abs((candidate_time - target).total_seconds()) / 3600))

    selected = min(candidates, key=distance_hours)
    return selected if distance_hours(selected) <= Decimal(str(match_tolerance_hours)) else None


def _latest_by_ticker(
    session: Session,
    model: Any,
    tickers: list[str],
    time_attr: str,
) -> dict[str, Any]:
    if not tickers:
        return {}
    column = getattr(model, time_attr)
    latest: dict[str, Any] = {}
    for ticker in dict.fromkeys(tickers):
        row = session.scalar(
            select(model)
            .where(model.ticker == ticker)
            .order_by(
                desc(column),
                desc(model.id) if hasattr(model, "id") else desc(column),
            )
            .limit(1)
        )
        if row is not None:
            latest[ticker] = row
    return latest


def _effective_location_key(location_key: str, settings: Settings) -> str:
    if location_key == "unknown":
        return normalize_location_key(settings.weather_v2_default_location_key)
    return normalize_location_key(location_key)


def _weather_feature_age_hours(feature: WeatherFeature, *, now: Any) -> Decimal | None:
    raw = decode_json(feature.raw_json)
    explicit = to_decimal(raw.get("forecast_age_hours"))
    if explicit is not None:
        return explicit
    generated_at = parse_datetime(raw.get("forecast_generated_at")) or _as_utc(feature.generated_at)
    return _age_hours(generated_at, now)


def _age_minutes(value: Any, now: Any) -> Decimal | None:
    timestamp = _as_utc(value)
    resolved_now = _as_utc(now)
    if timestamp is None or resolved_now is None:
        return None
    return max(Decimal("0"), Decimal(str((resolved_now - timestamp).total_seconds() / 60)))


def _age_hours(value: Any, now: Any) -> Decimal | None:
    timestamp = _as_utc(value)
    resolved_now = _as_utc(now)
    if timestamp is None or resolved_now is None:
        return None
    return max(Decimal("0"), Decimal(str((resolved_now - timestamp).total_seconds() / 3600)))


def _as_utc(value: Any) -> Any | None:
    return parse_datetime(value)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["first_blocker"] for row in rows)
    return {
        "current_weather_links": len(rows),
        "verified_kalshi_url_rows": sum(1 for row in rows if row["verified_kalshi_url"]),
        "fresh_snapshot_rows": sum(1 for row in rows if row["snapshot_fresh"]),
        "weather_source_rows": sum(
            1
            for row in rows
            if row["has_weather_source_forecast"] and row["weather_source_forecast_fresh"]
        ),
        "weather_feature_rows": sum(
            1 for row in rows if row["has_weather_feature"] and row["weather_feature_fresh"]
        ),
        "forecast_rows": sum(1 for row in rows if row["has_current_forecast"]),
        "ranking_rows": sum(1 for row in rows if row["has_current_ranking"]),
        "positive_raw_ev_rows": sum(
            1 for row in rows if (to_decimal(row.get("raw_ev")) or Decimal("0")) > 0
        ),
        "positive_executable_ev_rows": sum(
            1 for row in rows if (to_decimal(row.get("executable_ev")) or Decimal("0")) > 0
        ),
        "executable_book_rows": sum(1 for row in rows if row["executable_book"]),
        "phase3m_nonzero_rows": sum(1 for row in rows if row["phase3m_nonzero_size"]),
        "phase3n_approved_rows": sum(1 for row in rows if row["phase3n_approved"]),
        "paper_ready_rows": counts.get("PAPER_READY", 0),
        "first_hard_blocker": _first_hard_blocker(rows),
        "first_hard_blocker_counts": dict(counts),
        "rows_scanned": len(rows),
    }


def _first_hard_blocker(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NO_CURRENT_WEATHER_LINKS"
    for blocker in WEATHER_PAPER_BLOCKERS:
        if blocker != "PAPER_READY" and any(row["first_blocker"] == blocker for row in rows):
            return blocker
    return "PAPER_READY"


def _status(summary: dict[str, Any]) -> str:
    if summary["current_weather_links"] == 0:
        return "NO_CURRENT_WEATHER_LINKS"
    if summary["paper_ready_rows"] > 0:
        return "WEATHER_PAPER_READY"
    return "WEATHER_PAPER_GATE_BLOCKED"


def _acceptance(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "weather_rows_enter_gate_only_if_current_and_source_backed": True,
        "no_paper_trades_created": True,
        "no_live_or_demo_exchange_writes": True,
        "stale_3ap_only_truth_not_used": True,
        "exact_first_blocker_reported_if_closed": (
            summary["paper_ready_rows"] > 0 or summary["first_hard_blocker"] != "PAPER_READY"
        ),
    }


def _next_action(*, status: str, summary: dict[str, Any]) -> dict[str, Any]:
    if status == "WEATHER_PAPER_READY":
        return {
            "stage": "PAPER_ONLY_OPERATOR_REVIEW",
            "command": (
                "kalshi-bot phase3ap-paper-ready-unblock-report --output-dir "
                "reports/phase3ap --reports-dir reports"
            ),
            "reason": (
                "Weather has paper-ready candidates; refresh canonical 3AP before "
                "any operator review."
            ),
            "allow_paper_trade_creation": False,
        }
    if summary["first_hard_blocker"] == "MARKET_WINDOW_INELIGIBLE":
        command = (
            "kalshi-bot phase3ba-r3-weather-paper-gate --output-dir "
            "reports/phase3ba_r3 --reports-dir reports"
        )
        reason = (
            "Linked weather rows are outside the eligible entry window; wait for the next "
            "scheduled market window."
        )
    elif summary["first_hard_blocker"] == "LINK_UNVERIFIED":
        command = (
            "kalshi-bot phase3az-r13-weather-handoff-status --output-dir "
            "reports/phase3az_r13_weather --reports-dir reports"
        )
        reason = "Weather source evidence exists, but the exact Kalshi market link is not verified."
    elif summary["first_hard_blocker"] == "SNAPSHOT_MISSING":
        command = (
            "kalshi-bot db-writer-monitor --json && "
            "kalshi-bot capture-snapshots --status open --limit 100"
        )
        reason = "Weather gate is missing a market snapshot/orderbook."
    elif summary["first_hard_blocker"] == "SNAPSHOT_STALE":
        command = (
            "kalshi-bot db-writer-monitor --json && "
            "kalshi-bot capture-snapshots --status open --limit 100"
        )
        reason = "Weather gate is blocked by stale market snapshots/orderbooks."
    elif summary["first_hard_blocker"] == "EV_NOT_POSITIVE":
        command = (
            "kalshi-bot phase3ba-r3-weather-paper-gate --output-dir "
            "reports/phase3ba_r3 --reports-dir reports"
        )
        reason = "Weather is fully wired but current ranked rows have no positive raw EV."
    elif summary["first_hard_blocker"] == "FORECAST_MISSING":
        command = "kalshi-bot forecast --model weather_v2 --limit 500"
        reason = "Current weather links need fresh weather_v2 forecasts."
    elif summary["first_hard_blocker"] == "RANKING_MISSING":
        command = (
            "kalshi-bot phase3ba-r2-weather-ranking-activation --output-dir "
            "reports/phase3ba_r2 --reports-dir reports"
        )
        reason = "Current weather forecasts need weather_v2 rankings."
    else:
        command = (
            "kalshi-bot phase3az-r13-weather-handoff-status --output-dir "
            "reports/phase3az_r13_weather --reports-dir reports"
        )
        reason = f"Weather gate is blocked by {summary['first_hard_blocker']}."
    return {
        "stage": "KEEP_WEATHER_GATE_DIAGNOSTIC_ONLY",
        "command": command,
        "reason": reason,
        "allow_paper_trade_creation": False,
    }


def _funnel_steps() -> tuple[str, ...]:
    return (
        "current linked weather markets",
        "eligible market entry window",
        "verified Kalshi URL",
        "fresh weather snapshot/source evidence",
        "weather feature available",
        "weather forecast available",
        "weather ranking available",
        "positive raw EV",
        "positive executable EV",
        "book/liquidity available",
        "spread pass",
        "settlement terms known",
        "Phase 3S proceed",
        "Phase 3M nonzero size",
        "Phase 3N risk approval",
        "paper-ready candidate",
    )


def _metadata(
    session: Session,
    *,
    settings: Settings,
    generated_at: str,
    command_args: list[str],
    include_data_watermark: bool = True,
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
            "command": "kalshi-bot phase3ba-r3-weather-paper-gate",
            "argv": command_args,
        },
        "data_watermark": (
            _data_watermark(session)
            if include_data_watermark
            else {"status": "DEFERRED_FOR_DEADLINE_BUDGET"}
        ),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "safety_flags": _safety_flags(),
    }


def _data_watermark(session: Session) -> dict[str, Any]:
    return {
        "latest_market_seen_at": _latest_iso(session, Market.last_seen_at),
        "latest_snapshot_at": _latest_iso(session, MarketSnapshot.captured_at),
        "latest_weather_source_forecast_at": _latest_iso(
            session,
            WeatherForecast.forecast_generated_at,
        ),
        "latest_weather_feature_at": _latest_iso(session, WeatherFeature.generated_at),
        "latest_weather_v2_forecast_at": _latest_forecast_iso(session),
        "latest_weather_v2_ranking_at": _latest_ranking_iso(session),
        "latest_paper_order_at": _latest_iso(session, PaperOrder.created_at),
        "latest_paper_pnl_at": _latest_iso(session, PaperPnl.calculated_at),
    }


def _latest_iso(session: Session, column: Any) -> str | None:
    value = session.scalar(select(func.max(column)))
    return _iso_string(value)


def _latest_forecast_iso(session: Session) -> str | None:
    value = session.scalar(
        select(func.max(Forecast.forecasted_at)).where(Forecast.model_name == MODEL_NAME)
    )
    return _iso_string(value)


def _latest_ranking_iso(session: Session) -> str | None:
    value = session.scalar(
        select(func.max(MarketRanking.ranked_at)).where(MarketRanking.forecast_model == MODEL_NAME)
    )
    return _iso_string(value)


def _iso_string(value: Any) -> str | None:
    if value is None:
        return None
    formatter = getattr(value, "isoformat", None)
    return str(formatter()) if callable(formatter) else str(value)


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


def _safety_flags() -> dict[str, Any]:
    return {
        "paper_only": True,
        "diagnostic_only": True,
        "creates_rankings": False,
        "creates_opportunity_rows": False,
        "creates_paper_orders": False,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "live_trading_enabled": False,
        "demo_exchange_writes_enabled": False,
        "thresholds_lowered": False,
        "fabricates_weather_data": False,
        "fabricates_forecasts": False,
        "fabricates_urls": False,
        "fabricates_books": False,
        "fabricates_settlements": False,
        "uses_stale_3ap_only_truth": False,
    }


def _operator_guardrails() -> list[str]:
    return [
        "Keep PAPER / READ-ONLY.",
        "Do not create paper trades from this diagnostic phase.",
        "Do not submit, cancel, replace, or amend live/demo exchange orders.",
        "Do not lower EV, confidence, liquidity, spread, settlement, or risk thresholds.",
        "Do not fabricate weather evidence, URLs, books, opportunities, or settlements.",
        "Do not use stale paper-gate artifacts as current truth.",
    ]


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R3 Weather Paper Gate")
    summary = payload["summary"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{payload['status']}`",
            f"- Current weather links: `{summary['current_weather_links']}`",
            f"- Weather source-backed rows: `{summary['weather_source_rows']}`",
            f"- Forecast rows: `{summary['forecast_rows']}`",
            f"- Ranking rows: `{summary['ranking_rows']}`",
            f"- Positive raw EV rows: `{summary['positive_raw_ev_rows']}`",
            f"- Positive executable EV rows: `{summary['positive_executable_ev_rows']}`",
            f"- Executable book rows: `{summary['executable_book_rows']}`",
            f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            "",
            "## Next Action",
            "",
            f"- Stage: `{payload['next_action']['stage']}`",
            f"- Command: `{payload['next_action']['command']}`",
            f"- Paper trade creation allowed: "
            f"`{payload['next_action']['allow_paper_trade_creation']}`",
            f"- Reason: {payload['next_action']['reason']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R3 Weather Paper Gate Detail")
    lines.extend(["", "## Summary", ""])
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Weather Funnel",
            "",
        ]
    )
    for index, step in enumerate(payload["weather_paper_funnel"], start=1):
        lines.append(f"{index}. {step}")
    lines.extend(
        [
            "",
            "## Weather Rows",
            "",
            "| Ticker | Source | Snapshot | Forecast | Ranking | Raw EV | Exec EV | "
            "Book | Blocker |",
            "| --- | --- | --- | --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    if not payload["weather_rows"]:
        lines.append("| none |  |  |  |  |  |  |  | NO_CURRENT_WEATHER_LINKS |")
    for row in payload["weather_rows"]:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{row['weather_source_forecast_fresh']} | "
            f"{row['snapshot_fresh']} | "
            f"{row['has_current_forecast']} | "
            f"{row['has_current_ranking']} | "
            f"{row.get('raw_ev') or ''} | "
            f"{row.get('executable_ev') or ''} | "
            f"{row['executable_book']} | "
            f"{row['first_blocker']} |"
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R3 Next Actions")
    next_action = payload["next_action"]
    lines.extend(
        [
            "",
            "## Exact Next Operator Command",
            "",
            f"```bash\n{next_action['command']}\n```",
            "",
            f"- Stage: `{next_action['stage']}`",
            f"- Paper trade creation allowed: `{next_action['allow_paper_trade_creation']}`",
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
        f"- Thresholds lowered: `{payload['thresholds_lowered']}`",
    ]


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker",
        "location_key",
        "target_time",
        "verified_kalshi_url",
        "kalshi_url_status",
        "current_window_eligible",
        "has_snapshot",
        "snapshot_fresh",
        "snapshot_age_minutes",
        "has_weather_source_forecast",
        "weather_source_forecast_fresh",
        "has_weather_feature",
        "weather_feature_fresh",
        "has_current_forecast",
        "has_current_ranking",
        "raw_ev",
        "executable_ev",
        "executable_book",
        "settlement_terms_known",
        "phase3s_proceed",
        "phase3m_nonzero_size",
        "phase3m_proposed_contracts",
        "phase3n_approved",
        "first_blocker",
        "paper_ready",
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
