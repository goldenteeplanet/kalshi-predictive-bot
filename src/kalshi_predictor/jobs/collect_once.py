from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from sqlalchemy.orm import Session

from kalshi_predictor.data.db import describe_db_location, get_session_factory, init_db
from kalshi_predictor.data.repositories import decode_json, insert_forecast
from kalshi_predictor.forecasting.base import ForecastInput
from kalshi_predictor.forecasting.market_implied import MarketImpliedForecaster
from kalshi_predictor.ingest.markets import sync_markets
from kalshi_predictor.ingest.snapshots import capture_snapshots
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.memory.capture import capture_forecast_attempt
from kalshi_predictor.signals.attribution import attribute_forecast_signals

StagePageCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class CollectOnceSummary:
    markets_seen: int
    snapshots_inserted: int
    forecasts_inserted: int
    skipped_forecasts: int
    db_location: str
    collection_status: str = "COMPLETE"
    stopped_reason: str | None = None
    resume_cursor: str | None = None
    market_pages_processed: int = 0
    snapshot_pages_processed: int = 0
    collect_total_seconds: float = 0.0
    market_pages_seconds: float = 0.0
    orderbook_seconds: float = 0.0
    near_money_candidates: int = 0
    near_money_snapshots_inserted: int = 0
    liquidity_hint_candidates: int = 0
    liquidity_first_snapshots_inserted: int = 0
    no_liquidity_hint_snapshots_inserted: int = 0
    skipped_expired_windows: int = 0
    skipped_far_otm_rows: int = 0
    per_symbol_liquidity_first_counts: dict[str, int] = field(default_factory=dict)
    per_symbol_snapshot_counts: dict[str, int] = field(default_factory=dict)
    snapshot_tickers: list[str] = field(default_factory=list)
    rate_limit_status: str = "COMPLETE"
    rate_limited: bool = False
    rate_limit_details: dict[str, Any] = field(default_factory=dict)
    data_complete: bool = True


def collect_once(
    *,
    status: str | None = "open",
    limit: int = 100,
    max_pages: int | None = 1,
    series_ticker: str | None = None,
    event_ticker: str | None = None,
    start_cursor: str | None = None,
    deadline_monotonic: float | None = None,
    page_callback: StagePageCallback | None = None,
    include_orderbook: bool = True,
    generate_market_implied_forecasts: bool = True,
    db_url: str | None = None,
    session: Session | None = None,
    client: KalshiClient | None = None,
    console: Console | None = None,
) -> CollectOnceSummary:
    owns_session = session is None
    owns_client = client is None
    page_state: dict[str, dict[str, Any]] = {
        "market_sync": {},
        "snapshot_capture": {},
    }

    if session is None:
        engine = init_db(db_url)
        session = get_session_factory(engine)()
    if client is None:
        client = KalshiClient()

    try:
        markets_seen = sync_markets(
            status=status,
            max_pages=max_pages,
            limit=limit,
            series_ticker=series_ticker,
            event_ticker=event_ticker,
            start_cursor=start_cursor,
            deadline_monotonic=deadline_monotonic,
            page_callback=_stage_page_callback(
                "market_sync",
                page_state,
                page_callback,
            ),
            session=session,
            client=client,
        )
        snapshots = capture_snapshots(
            status=status,
            max_pages=max_pages,
            limit=limit,
            series_ticker=series_ticker,
            event_ticker=event_ticker,
            start_cursor=start_cursor,
            deadline_monotonic=deadline_monotonic,
            page_callback=_stage_page_callback(
                "snapshot_capture",
                page_state,
                page_callback,
            ),
            include_orderbook=include_orderbook,
            session=session,
            client=client,
        )

        forecasts_inserted = 0
        skipped_forecasts = 0
        if generate_market_implied_forecasts:
            forecaster = MarketImpliedForecaster()
            for snapshot in snapshots:
                forecast_input = ForecastInput(
                    ticker=snapshot.ticker,
                    captured_at=snapshot.captured_at,
                    market_json=decode_json(snapshot.raw_market_json),
                    orderbook_json=decode_json(snapshot.raw_orderbook_json),
                )
                forecast = forecaster.forecast(forecast_input)
                if forecast is None:
                    capture_forecast_attempt(
                        session,
                        snapshot=snapshot,
                        model_name=forecaster.model_name,
                        forecast=None,
                    )
                    skipped_forecasts += 1
                    continue
                record = insert_forecast(session, forecast)
                session.flush()
                attribute_forecast_signals(session, record)
                forecasts_inserted += 1

        if owns_session:
            session.commit()
    except Exception:
        if owns_session:
            session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
        if owns_client:
            client.close()

    collection_state = _collection_state(page_state)
    rate_limit_details = client.telemetry.as_dict(
        rows_fetched_before_limit=markets_seen + len(snapshots),
    )
    rate_limit_status = str(rate_limit_details.get("status") or "COMPLETE")
    rate_limited = bool(rate_limit_details.get("rate_limited"))
    collection_status = (
        rate_limit_status
        if rate_limited or rate_limit_status != "COMPLETE"
        else collection_state["collection_status"]
    )
    summary = CollectOnceSummary(
        markets_seen=markets_seen,
        snapshots_inserted=len(snapshots),
        forecasts_inserted=forecasts_inserted,
        skipped_forecasts=skipped_forecasts,
        db_location=describe_db_location(db_url),
        collection_status=collection_status,
        stopped_reason=collection_state["stopped_reason"],
        resume_cursor=collection_state["resume_cursor"],
        market_pages_processed=int(page_state["market_sync"].get("pages_processed") or 0),
        snapshot_pages_processed=int(
            page_state["snapshot_capture"].get("pages_processed") or 0
        ),
        rate_limit_status=rate_limit_status,
        rate_limited=rate_limited,
        rate_limit_details=rate_limit_details,
        data_complete=not rate_limited,
    )
    _print_summary(summary, console=console)
    return summary


def _print_summary(summary: CollectOnceSummary, *, console: Console | None = None) -> None:
    resolved_console = console or Console()
    resolved_console.print("[bold]Kalshi Phase 1 collection summary[/bold]")
    resolved_console.print(f"Markets seen: {summary.markets_seen}")
    resolved_console.print(f"Snapshots inserted: {summary.snapshots_inserted}")
    resolved_console.print(f"Forecasts inserted: {summary.forecasts_inserted}")
    resolved_console.print(f"Skipped forecasts: {summary.skipped_forecasts}")
    resolved_console.print(f"Collection status: {summary.collection_status}")
    if summary.resume_cursor:
        resolved_console.print(f"Resume cursor: {summary.resume_cursor}")
    resolved_console.print(f"DB location: {Path(summary.db_location)}")


def _stage_page_callback(
    stage: str,
    page_state: dict[str, dict[str, Any]],
    outer_callback: StagePageCallback | None,
) -> Callable[[dict[str, Any]], None]:
    def callback(payload: dict[str, Any]) -> None:
        state = page_state[stage]
        event = payload.get("event")
        if event == "page":
            state["pages_processed"] = int(payload.get("pages_seen") or 0)
            state["resume_cursor"] = payload.get("resume_cursor")
            state["has_more"] = bool(payload.get("has_more"))
            state["markets_seen"] = int(state.get("markets_seen") or 0) + int(
                payload.get("markets_on_page") or 0
            )
        elif event == "stop":
            state["stopped_reason"] = payload.get("stop_reason")
            state["resume_cursor"] = payload.get("resume_cursor")
        if outer_callback is not None:
            outer_callback(stage, payload)

    return callback


def _collection_state(page_state: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    for stage in ("market_sync", "snapshot_capture"):
        state = page_state[stage]
        stopped_reason = state.get("stopped_reason")
        if stopped_reason:
            return {
                "collection_status": (
                    "TIMED_OUT_CLEANLY"
                    if stopped_reason == "deadline"
                    else "PARTIAL_REFRESH_CONTINUABLE"
                ),
                "stopped_reason": str(stopped_reason),
                "resume_cursor": _string_or_none(state.get("resume_cursor")),
            }
    for stage in ("snapshot_capture", "market_sync"):
        state = page_state[stage]
        if state.get("has_more"):
            return {
                "collection_status": "PARTIAL_REFRESH_CONTINUABLE",
                "stopped_reason": "page_window_complete",
                "resume_cursor": _string_or_none(state.get("resume_cursor")),
            }
    return {
        "collection_status": "COMPLETE",
        "stopped_reason": None,
        "resume_cursor": None,
    }


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
