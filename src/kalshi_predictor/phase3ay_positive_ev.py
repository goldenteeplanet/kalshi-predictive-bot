from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, aliased

from kalshi_predictor.active_universe import is_active_market_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import SUPPORTED_CRYPTO_ASSETS, symbol_from_event_ticker
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PositionSizingDecisionLog,
)
from kalshi_predictor.kalshi.orderbook import usable_bid_ask_book
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.opportunities.scanner import build_market_ranking
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ap import _phase3ap_gate_row
from kalshi_predictor.phase3ar import repair_crypto_snapshots_for_tickers
from kalshi_predictor.phase3bc_r5 import MODEL_NAME
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3AY_ACCELERATOR_VERSION = "phase3ay_positive_ev_accelerator_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3ay")
DEFAULT_REPORTS_DIR = Path("reports")


@dataclass(frozen=True)
class Phase3AYPositiveEVArtifacts:
    output_dir: Path
    executive_summary_path: Path
    near_miss_rows_path: Path
    json_path: Path
    next_actions_path: Path
    manifest_path: Path


def build_phase3ay_positive_ev_accelerator(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    symbols: str = "BTC,ETH,XRP,DOGE",
    near_miss_cents: Decimal = Decimal("1.0"),
    max_candidates: int = 50,
    refresh_snapshots: bool = True,
    allow_concurrent_refresh: bool = False,
    registered_commands: set[str] | None = None,
) -> dict[str, Any]:
    resolved = learning_paper_settings(settings or get_settings())
    now = utc_now()
    requested_symbols = _parse_symbols(symbols)
    before_rows = _rank_current_crypto_rows(
        session,
        settings=resolved,
        symbols=requested_symbols,
        now=now,
    )
    near_miss_before = _near_miss_rows(
        before_rows,
        near_miss_cents=near_miss_cents,
        max_candidates=max_candidates,
    )
    refresh_result = _refresh_near_miss_snapshots(
        session,
        near_miss_before,
        reports_dir=reports_dir,
        enabled=refresh_snapshots,
        allow_concurrent_refresh=allow_concurrent_refresh,
        max_candidates=max_candidates,
    )
    after_rows = (
        _rank_current_crypto_rows(
            session,
            settings=resolved,
            symbols=requested_symbols,
            now=utc_now(),
        )
        if refresh_result.get("recompute_after_refresh")
        else before_rows
    )
    after_by_ticker = {str(row["ticker"]): row for row in after_rows}
    tracked_after = [
        after_by_ticker[ticker]
        for ticker in [str(row["ticker"]) for row in near_miss_before]
        if ticker in after_by_ticker
    ]
    positive_after = [row for row in tracked_after if _ev_value(row) > Decimal("0")]
    executable_positive = [row for row in positive_after if row["clean_executable_book"]]
    paper_gate_rows = _paper_ready_gate_rows(
        session,
        positive_after,
        settings=resolved,
        now=utc_now(),
    )
    command_audit = _command_audit(registered_commands or set())
    summary = _summary(
        before_rows=before_rows,
        near_miss_before=near_miss_before,
        tracked_after=tracked_after,
        positive_after=positive_after,
        executable_positive=executable_positive,
        paper_gate_rows=paper_gate_rows,
        refresh_result=refresh_result,
        near_miss_cents=near_miss_cents,
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AY",
        "phase_version": PHASE3AY_ACCELERATOR_VERSION,
        "mode": "PAPER_ONLY_POSITIVE_EV_NEAR_MISS_ACCELERATOR",
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "fabricated_evidence": False,
        "settings": {
            "symbols": sorted(requested_symbols),
            "near_miss_cents": decimal_to_str(near_miss_cents),
            "max_candidates": max_candidates,
            "model_name": MODEL_NAME,
            "refresh_snapshots": refresh_snapshots,
            "allow_concurrent_refresh": allow_concurrent_refresh,
        },
        "summary": summary,
        "near_miss_rows": near_miss_before,
        "post_refresh_tracked_rows": tracked_after,
        "positive_ev_rows_after_refresh": positive_after,
        "paper_ready_gate_rows": paper_gate_rows,
        "refresh_result": refresh_result,
        "command_registry_audit": command_audit,
        "next_actions": _registered_next_actions(command_audit, summary),
        "next_codex_task": _next_codex_task(summary),
        "operator_do_not_run": [
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not create paper trades from this accelerator.",
            (
                "Do not lower EV, confidence, liquidity, spread, score, settlement, "
                "or risk thresholds."
            ),
            "Do not include expired, synthetic, composite, sibling, fuzzy, or historical rows.",
        ],
    }


def write_phase3ay_positive_ev_accelerator_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    symbols: str = "BTC,ETH,XRP,DOGE",
    near_miss_cents: Decimal = Decimal("1.0"),
    max_candidates: int = 50,
    refresh_snapshots: bool = True,
    allow_concurrent_refresh: bool = False,
    registered_commands: set[str] | None = None,
) -> Phase3AYPositiveEVArtifacts:
    payload = build_phase3ay_positive_ev_accelerator(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        symbols=symbols,
        near_miss_cents=near_miss_cents,
        max_candidates=max_candidates,
        refresh_snapshots=refresh_snapshots,
        allow_concurrent_refresh=allow_concurrent_refresh,
        registered_commands=registered_commands,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    near_miss_rows_path = output_dir / "near_miss_rows.csv"
    json_path = output_dir / "positive_ev_acceleration.json"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"
    _write_json(json_path, payload)
    _write_near_miss_csv(near_miss_rows_path, payload["near_miss_rows"])
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [executive_summary_path, near_miss_rows_path, json_path, next_actions_path],
    )
    return Phase3AYPositiveEVArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        near_miss_rows_path=near_miss_rows_path,
        json_path=json_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def _rank_current_crypto_rows(
    session: Session,
    *,
    settings: Settings,
    symbols: set[str],
    now: datetime,
) -> list[dict[str, Any]]:
    markets = _current_crypto_markets_by_ticker(session, symbols=symbols, now=now)
    forecasts = _latest_forecasts_for_tickers(session, sorted(markets))
    snapshots = _latest_snapshots_for_tickers(session, sorted(markets))
    rows: list[dict[str, Any]] = []
    for ticker in sorted(markets):
        market = markets[ticker]
        forecast = forecasts.get(ticker)
        snapshot = snapshots.get(ticker)
        if forecast is None or snapshot is None:
            continue
        if not _current_pure_crypto_market(market, ticker, symbols=symbols, now=now):
            continue
        ranking = build_market_ranking(
            forecast=forecast,
            snapshot=snapshot,
            settings=settings,
            ranked_at=now,
        )
        rows.append(_row_payload(market, forecast, snapshot, ranking, now=now, settings=settings))
    return sorted(rows, key=_near_miss_sort_key, reverse=True)


def _row_payload(
    market: Market | None,
    forecast: Forecast,
    snapshot: MarketSnapshot,
    ranking: dict[str, Any],
    *,
    now: datetime,
    settings: Settings,
) -> dict[str, Any]:
    expected_value = to_decimal(ranking.get("expected_value"))
    spread = to_decimal(ranking.get("spread"))
    liquidity = to_decimal(ranking.get("liquidity")) or Decimal("0")
    liquidity_score = to_decimal(ranking.get("liquidity_score")) or Decimal("0")
    close_time = _market_close_time(market)
    time_remaining = (
        Decimal(str((close_time - now).total_seconds() / 60))
        if close_time is not None
        else to_decimal(ranking.get("time_to_close_minutes"))
    )
    raw_orderbook = decode_json(snapshot.raw_orderbook_json)
    book = usable_bid_ask_book(
        raw_orderbook,
        side=str(ranking.get("best_side") or ""),
        liquidity_score=ranking.get("liquidity_score"),
        max_spread=settings.opportunity_max_spread,
    )
    clean_book = bool(book.usable)
    return {
        "ticker": forecast.ticker,
        "market_ticker": forecast.ticker,
        "symbol": _symbol_for_market(market, forecast.ticker),
        "title": ranking.get("title") or (market.title if market else None),
        "event_ticker": market.event_ticker if market else ranking.get("event_ticker"),
        "series_ticker": market.series_ticker if market else ranking.get("series_ticker"),
        "market_status": market.status if market else snapshot.status,
        "close_time": close_time.isoformat() if close_time else None,
        "expected_expiration_time": (
            market.expected_expiration_time.isoformat()
            if market and market.expected_expiration_time
            else None
        ),
        "latest_forecast_at": forecast.forecasted_at.isoformat(),
        "latest_snapshot_at": snapshot.captured_at.isoformat(),
        "forecast_probability": ranking.get("forecast_probability"),
        "best_side": ranking.get("best_side"),
        "best_price": ranking.get("best_price"),
        "expected_value": decimal_to_str(expected_value),
        "expected_value_cents": _cents(expected_value),
        "gap_to_positive_cents": _gap_to_positive_cents(expected_value),
        "estimated_edge": ranking.get("estimated_edge"),
        "opportunity_score": ranking.get("opportunity_score"),
        "model_confidence_score": ranking.get("model_confidence_score"),
        "spread": decimal_to_str(spread),
        "liquidity": decimal_to_str(liquidity),
        "liquidity_score": decimal_to_str(liquidity_score),
        "spread_score": ranking.get("spread_score"),
        "time_to_close_minutes": decimal_to_str(time_remaining),
        "nonzero_liquidity": liquidity > 0 or liquidity_score > 0,
        "tight_spread": spread is not None and spread <= settings.opportunity_max_spread,
        "clean_executable_book": clean_book,
        "book_state": book.state,
        "book_reason": book.reason,
        "active_market": True,
        "structure_status": "PURE_CRYPTO",
        "ranking": ranking,
        "ranking_source": "RECOMPUTED_FROM_LATEST_DB_FORECAST_AND_SNAPSHOT",
    }


def _near_miss_rows(
    rows: list[dict[str, Any]],
    *,
    near_miss_cents: Decimal,
    max_candidates: int,
) -> list[dict[str, Any]]:
    band = near_miss_cents / Decimal("100")
    near_misses = [
        row
        for row in rows
        if (value := _ev_value(row)) <= 0 and value >= -band
    ]
    return sorted(near_misses, key=_near_miss_sort_key, reverse=True)[:max(0, max_candidates)]


def _refresh_near_miss_snapshots(
    session: Session,
    near_miss_rows: list[dict[str, Any]],
    *,
    reports_dir: Path,
    enabled: bool,
    allow_concurrent_refresh: bool,
    max_candidates: int,
) -> dict[str, Any]:
    tickers = [str(row["ticker"]) for row in near_miss_rows[:max(0, max_candidates)]]
    if not enabled:
        return _refresh_status("DISABLED", tickers=tickers)
    if not tickers:
        return _refresh_status("NO_NEAR_MISS_ROWS", tickers=tickers)
    watcher = _active_crypto_watcher_state(reports_dir)
    if watcher["active"] and not allow_concurrent_refresh:
        result = _refresh_status("SKIPPED_ACTIVE_CRYPTO_WATCHER", tickers=tickers)
        result["watcher_state"] = watcher
        return result
    result = repair_crypto_snapshots_for_tickers(session, tickers, limit=len(tickers))
    result["status"] = "REFRESH_ATTEMPTED"
    result["refresh_scope"] = "NEAR_MISS_ONLY"
    result["candidate_tickers"] = tickers
    result["recompute_after_refresh"] = True
    result["paper_only"] = True
    result["live_or_demo_execution"] = False
    result["paper_trade_creation"] = False
    return result


def _paper_ready_gate_rows(
    session: Session,
    rows: list[dict[str, Any]],
    *,
    settings: Settings,
    now: datetime,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    tickers = [str(row["ticker"]) for row in rows]
    forecasts = _latest_forecasts_for_tickers(session, tickers)
    snapshots = {ticker: _latest_snapshot(session, ticker) for ticker in tickers}
    sizing = _latest_by_ticker(session, PositionSizingDecisionLog, tickers, "decision_timestamp")
    risk = _latest_by_ticker(session, AdvancedRiskDecisionLog, tickers, "decision_timestamp")
    paper_orders = _paper_order_keys(session, tickers)
    gate_rows: list[dict[str, Any]] = []
    for row in rows:
        ranking = _market_ranking_from_payload(row, now=now)
        snapshot = snapshots.get(str(row["ticker"]))
        if snapshot is None:
            continue
        gate_rows.append(
            _phase3ap_gate_row(
                session,
                ranking,
                forecast=forecasts.get(str(row["ticker"])),
                snapshot=snapshot,
                sizing=sizing.get(str(row["ticker"])),
                risk=risk.get(str(row["ticker"])),
                paper_orders=paper_orders,
                now=now,
                settings=settings,
            )
        )
    return gate_rows


def _market_ranking_from_payload(row: dict[str, Any], *, now: datetime) -> MarketRanking:
    ranking = row.get("ranking") if isinstance(row.get("ranking"), dict) else {}
    return MarketRanking(
        ticker=str(row["ticker"]),
        ranked_at=now,
        title=str(row.get("title") or ""),
        status=str(row.get("market_status") or ""),
        series_ticker=row.get("series_ticker"),
        event_ticker=row.get("event_ticker"),
        volume=ranking.get("volume"),
        open_interest=ranking.get("open_interest"),
        liquidity=row.get("liquidity"),
        spread=row.get("spread"),
        midpoint=ranking.get("midpoint"),
        time_to_close_minutes=row.get("time_to_close_minutes"),
        forecast_model=MODEL_NAME,
        forecast_probability=row.get("forecast_probability"),
        best_side=row.get("best_side"),
        best_price=row.get("best_price"),
        estimated_edge=row.get("estimated_edge"),
        liquidity_score=str(row.get("liquidity_score") or "0"),
        spread_score=str(row.get("spread_score") or "0"),
        time_score=str(ranking.get("time_score") or "0"),
        model_confidence_score=str(row.get("model_confidence_score") or "0"),
        opportunity_score=str(row.get("opportunity_score") or "0"),
        reason=str(ranking.get("reason") or "Phase 3AY near-miss in-memory gate row."),
        raw_json=json.dumps(ranking.get("raw_json") or ranking, sort_keys=True, default=str),
    )


def _summary(
    *,
    before_rows: list[dict[str, Any]],
    near_miss_before: list[dict[str, Any]],
    tracked_after: list[dict[str, Any]],
    positive_after: list[dict[str, Any]],
    executable_positive: list[dict[str, Any]],
    paper_gate_rows: list[dict[str, Any]],
    refresh_result: dict[str, Any],
    near_miss_cents: Decimal,
) -> dict[str, Any]:
    best_before = before_rows[0] if before_rows else None
    best_near = near_miss_before[0] if near_miss_before else None
    paper_ready_rows = [row for row in paper_gate_rows if row.get("paper_ready")]
    first_blocker = _first_blocker(
        near_miss_rows=near_miss_before,
        positive_rows=positive_after,
        executable_positive=executable_positive,
        paper_ready_rows=paper_ready_rows,
        refresh_result=refresh_result,
    )
    return {
        "current_crypto_rows_scored": len(before_rows),
        "current_near_miss_rows": len(near_miss_before),
        "near_miss_band_cents": decimal_to_str(near_miss_cents),
        "best_current_ticker": best_before.get("ticker") if best_before else None,
        "best_current_ev_cents": best_before.get("expected_value_cents") if best_before else None,
        "best_ev_gap_cents": (
            best_near.get("gap_to_positive_cents")
            if best_near
            else (best_before.get("gap_to_positive_cents") if best_before else None)
        ),
        "closest_near_miss_tickers": [str(row["ticker"]) for row in near_miss_before[:10]],
        "refresh_status": refresh_result.get("status"),
        "refresh_attempted": refresh_result.get("status") == "REFRESH_ATTEMPTED",
        "post_refresh_rows_recomputed": len(tracked_after),
        "positive_ev_crossed_after_refresh": len(positive_after),
        "positive_ev_executable_rows": len(executable_positive),
        "paper_ready_after_3s_3m_3n": len(paper_ready_rows),
        "first_hard_blocker": first_blocker,
        "operator_next_action": _operator_next_action(first_blocker),
        "thresholds_lowered": False,
        "paper_trade_creation": False,
        "live_or_demo_execution": False,
    }


def _first_blocker(
    *,
    near_miss_rows: list[dict[str, Any]],
    positive_rows: list[dict[str, Any]],
    executable_positive: list[dict[str, Any]],
    paper_ready_rows: list[dict[str, Any]],
    refresh_result: dict[str, Any],
) -> str:
    if paper_ready_rows:
        return "PAPER_READY_CANDIDATE_AVAILABLE"
    if executable_positive:
        return "POSITIVE_EV_EXECUTABLE_REQUIRES_PAPER_READY_GATE"
    if positive_rows:
        return "POSITIVE_EV_NO_EXECUTABLE_BOOK_OR_GATE_BLOCKED"
    if near_miss_rows:
        if refresh_result.get("status") == "SKIPPED_ACTIVE_CRYPTO_WATCHER":
            return "NEAR_MISS_WAIT_FOR_ACTIVE_R5_REFRESH"
        return "NEAR_MISS_NO_POSITIVE_EV"
    return "NO_CURRENT_NEAR_MISS_ROWS"


def _operator_next_action(first_blocker: str) -> str:
    if first_blocker == "PAPER_READY_CANDIDATE_AVAILABLE":
        return "Run the canonical R5 status check, then only proceed through paper gates."
    if first_blocker == "NEAR_MISS_WAIT_FOR_ACTIVE_R5_REFRESH":
        return "Wait for the active R5 watcher to finish its current refresh cycle."
    if first_blocker == "NEAR_MISS_NO_POSITIVE_EV":
        return "Keep watching; no strict positive EV crossed the gate."
    return "Run phase3bc-r5-status and wait for a current positive-EV row."


def _current_pure_crypto_market(
    market: Market | None,
    ticker: str,
    *,
    symbols: set[str],
    now: datetime,
) -> bool:
    if market is None:
        return False
    symbol = _symbol_for_market(market, ticker)
    if symbol not in symbols:
        return False
    if not is_active_market_status(market.status):
        return False
    if market.result or market.settlement_ts:
        return False
    close_time = _market_close_time(market)
    if close_time is not None and close_time <= now:
        return False
    expected_expiration = _ensure_utc(market.expected_expiration_time)
    if expected_expiration is not None and expected_expiration <= now:
        return False
    text = f"{ticker} {market.title or ''} {market.series_ticker or ''}".lower()
    return not any(token in text for token in ("synthetic", "composite", "crosscategory"))


def _current_crypto_markets_by_ticker(
    session: Session,
    *,
    symbols: set[str],
    now: datetime,
) -> dict[str, Market]:
    prefixes = _crypto_prefixes_for_symbols(symbols)
    if not prefixes:
        return {}
    crypto_scope = or_(
        Market.series_ticker.in_(prefixes),
        Market.event_ticker.in_(prefixes),
        *[Market.ticker.like(f"{prefix}%") for prefix in prefixes],
        *[Market.event_ticker.like(f"{prefix}%") for prefix in prefixes],
    )
    rows = session.scalars(
        select(Market)
        .where(
            crypto_scope,
            func.lower(Market.status).in_(["active", "open"]),
            Market.result.is_(None),
            Market.settlement_ts.is_(None),
            or_(Market.close_time.is_(None), Market.close_time > now),
            or_(Market.expected_expiration_time.is_(None), Market.expected_expiration_time > now),
        )
        .order_by(Market.ticker)
    )
    return {market.ticker: market for market in rows}


def _crypto_prefixes_for_symbols(symbols: set[str]) -> tuple[str, ...]:
    prefixes = [
        prefix
        for asset in SUPPORTED_CRYPTO_ASSETS
        if asset.symbol in symbols
        for prefix in asset.event_prefixes
    ]
    return tuple(sorted(set(prefixes)))


def _market_close_time(market: Market | None) -> datetime | None:
    if market is None:
        return None
    return _ensure_utc(market.close_time)


def _ensure_utc(value: Any) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _symbol_for_market(market: Market | None, ticker: str) -> str | None:
    if market is not None:
        return symbol_from_event_ticker(market.event_ticker or market.series_ticker or ticker)
    return symbol_from_event_ticker(ticker)


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_forecasts_for_tickers(
    session: Session,
    tickers: list[str],
) -> dict[str, Forecast]:
    unique_tickers = sorted({ticker for ticker in tickers if ticker})
    if not unique_tickers:
        return {}
    statement = (
        select(
            Forecast,
            func.row_number()
            .over(
                partition_by=Forecast.ticker,
                order_by=(desc(Forecast.forecasted_at), desc(Forecast.id)),
            )
            .label("row_number"),
        )
        .where(Forecast.model_name == MODEL_NAME, Forecast.ticker.in_(unique_tickers))
        .subquery()
    )
    forecast = aliased(Forecast, statement)
    return {
        row.ticker: row
        for row in session.scalars(select(forecast).where(statement.c.row_number == 1))
    }


def _latest_snapshots_for_tickers(
    session: Session,
    tickers: list[str],
) -> dict[str, MarketSnapshot]:
    unique_tickers = sorted({ticker for ticker in tickers if ticker})
    if not unique_tickers:
        return {}
    statement = (
        select(
            MarketSnapshot,
            func.row_number()
            .over(
                partition_by=MarketSnapshot.ticker,
                order_by=(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id)),
            )
            .label("row_number"),
        )
        .where(MarketSnapshot.ticker.in_(unique_tickers))
        .subquery()
    )
    snapshot = aliased(MarketSnapshot, statement)
    return {
        row.ticker: row
        for row in session.scalars(select(snapshot).where(statement.c.row_number == 1))
    }


def _latest_by_ticker(
    session: Session,
    model: Any,
    tickers: list[str],
    time_attr: str,
) -> dict[str, Any]:
    if not tickers:
        return {}
    column = getattr(model, time_attr)
    rows = session.scalars(
        select(model)
        .where(model.ticker.in_(tickers))
        .order_by(model.ticker, desc(column), desc(model.id))
    )
    latest: dict[str, Any] = {}
    for row in rows:
        latest.setdefault(row.ticker, row)
    return latest


def _paper_order_keys(session: Session, tickers: list[str]) -> set[tuple[str, str, int | None]]:
    if not tickers:
        return set()
    rows = session.scalars(select(PaperOrder).where(PaperOrder.ticker.in_(tickers)))
    return {(row.ticker, row.model_name, row.forecast_id) for row in rows}


def _near_miss_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    ev = _ev_value(row)
    spread = to_decimal(row.get("spread"))
    inverse_spread = -spread if spread is not None else Decimal("-999")
    return (
        ev,
        Decimal("1") if row.get("nonzero_liquidity") else Decimal("0"),
        inverse_spread,
        Decimal("1") if row.get("clean_executable_book") else Decimal("0"),
        to_decimal(row.get("model_confidence_score")) or Decimal("0"),
    )


def _ev_value(row: dict[str, Any]) -> Decimal:
    return to_decimal(row.get("expected_value")) or Decimal("-999")


def _cents(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_to_str((value * Decimal("100")).quantize(Decimal("0.1")))


def _gap_to_positive_cents(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value > 0:
        return "0.0"
    return _cents(-value)


def _parse_symbols(symbols: str) -> set[str]:
    return {
        token.strip().upper()
        for token in symbols.split(",")
        if token.strip()
    }


def _active_crypto_watcher_state(reports_dir: Path) -> dict[str, Any]:
    status = _read_json(reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json")
    watch = _read_json(
        reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json"
    )
    process = status.get("process") if isinstance(status.get("process"), dict) else {}
    guard = status.get("guard") if isinstance(status.get("guard"), dict) else {}
    latest = status.get("latest_summary") if isinstance(status.get("latest_summary"), dict) else {}
    active = (
        process.get("status") == "RUNNING"
        or guard.get("status") == "RUNNING"
        or process.get("phase3bc_r5_process_running") is True
    )
    return {
        "active": active,
        "process_status": process.get("status"),
        "guard_status": guard.get("status"),
        "watch_state": latest.get("watch_state") or (watch.get("summary") or {}).get("watch_state"),
        "source": "reports/phase3bc_r5",
    }


def _refresh_status(status: str, *, tickers: list[str]) -> dict[str, Any]:
    return {
        "status": status,
        "refresh_scope": "NEAR_MISS_ONLY",
        "candidate_tickers": tickers,
        "requested": len(tickers),
        "attempted": 0,
        "repaired": 0,
        "recompute_after_refresh": False,
        "paper_only": True,
        "live_or_demo_execution": False,
        "paper_trade_creation": False,
    }


def _command_audit(registered_commands: set[str]) -> dict[str, Any]:
    commands = [
        (
            "kalshi-bot phase3ay-positive-ev-accelerator --output-dir reports/phase3ay "
            "--symbols BTC,ETH,XRP,DOGE --near-miss-cents 1.0 --max-candidates 50"
        ),
        "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5",
        "kalshi-bot phase3ap-paper-ready-unblock-report --output-dir reports/phase3ap",
        "kalshi-bot phase3ax-gap-analysis --output-dir reports/phase3ax --reports-dir reports",
    ]
    rows = []
    for command in commands:
        name = _command_name(command)
        rows.append(
            {
                "command": name,
                "full_command": command,
                "registered": name in registered_commands,
                "included_in_next_actions": name in registered_commands,
            }
        )
    return {
        "candidate_commands": rows,
        "missing_command_names": [row["command"] for row in rows if not row["registered"]],
        "next_actions_reference_only_registered_commands": True,
    }


def _registered_next_actions(
    command_audit: dict[str, Any],
    summary: dict[str, Any],
) -> list[str]:
    commands = [
        str(row["full_command"])
        for row in command_audit["candidate_commands"]
        if row.get("registered")
    ]
    if summary.get("positive_ev_crossed_after_refresh", 0) <= 0:
        return [
            command
            for command in commands
            if "phase3bc-r5-status" in command
            or "phase3ay-positive-ev-accelerator" in command
            or "phase3ax-gap-analysis" in command
        ]
    return commands


def _next_codex_task(summary: dict[str, Any]) -> dict[str, Any]:
    if int(summary.get("paper_ready_after_3s_3m_3n") or 0) > 0:
        phase = "Phase 3AP-R1 Paper-Ready Candidate Review"
        reason = "A positive-EV row passed paper-ready gates and needs operator review."
        problem = "Confirm paper-only readiness without creating trades from diagnostics."
    else:
        phase = "Phase 3AH-R3 Sports Provenance Repair"
        reason = "Crypto remains correctly gated while waiting for strict positive EV."
        problem = "Move to the next code-repairable app gap while the watcher waits."
    return {
        "task_phase_name": phase,
        "reason": reason,
        "problem_statement": problem,
        "acceptance_criteria": [
            "Keep everything PAPER / READ-ONLY.",
            "Do not submit/cancel/replace/amend live or demo exchange orders.",
            "Do not create paper trades from diagnostics.",
            "Do not lower thresholds or fabricate evidence.",
        ],
        "estimated_risk_level": "MEDIUM",
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    near = payload["near_miss_rows"][:10]
    lines = [
        "# Phase 3AY Positive EV Acceleration",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        "",
        "## Answers",
        "",
        f"1. Current near-miss rows: `{summary['current_near_miss_rows']}`.",
        f"2. Best EV gap: `{summary['best_ev_gap_cents']}` cents.",
        "3. Closest rows: "
        + (", ".join(str(row["ticker"]) for row in near) if near else "`none`")
        + ".",
        (
            "4. Rows crossing into positive EV after refresh: "
            f"`{summary['positive_ev_crossed_after_refresh']}`."
        ),
        f"5. Positive-EV executable rows: `{summary['positive_ev_executable_rows']}`.",
        f"6. Paper-ready after 3S/3M/3N: `{summary['paper_ready_after_3s_3m_3n']}`.",
        f"7. Operator next action: {summary['operator_next_action']}",
        "",
        "No live/demo exchange writes, paper trades, threshold changes, or fabricated "
        "evidence were produced by this command.",
        "",
    ]
    return "\n".join(lines)


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AY Next Actions",
        "",
        f"- First hard blocker: `{payload['summary']['first_hard_blocker']}`",
        f"- Refresh status: `{payload['summary']['refresh_status']}`",
        f"- Next Codex task: `{payload['next_codex_task']['task_phase_name']}`",
        "",
        "## Registered Commands",
        "",
    ]
    if payload["next_actions"]:
        lines.extend(f"- `{command}`" for command in payload["next_actions"])
    else:
        lines.append("- No registered command recommendations are available.")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Do not force paper trades.",
            "- Do not lower thresholds.",
            "- Do not run live/demo exchange writes.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_near_miss_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ticker",
        "symbol",
        "expected_value_cents",
        "gap_to_positive_cents",
        "best_side",
        "best_price",
        "liquidity_score",
        "spread",
        "clean_executable_book",
        "time_to_close_minutes",
        "model_confidence_score",
        "latest_snapshot_at",
        "close_time",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for artifact in files:
        if artifact.exists():
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            lines.append(f"{digest}  {artifact.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _command_name(command: str) -> str:
    parts = command.split()
    return parts[1] if len(parts) > 1 and parts[0] == "kalshi-bot" else parts[0]
