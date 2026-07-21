from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from kalshi_predictor.config import Settings
from kalshi_predictor.ingest.websocket_orderbooks import (
    StreamSummary,
    adapter_from_settings,
)
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.utils.time import utc_now

DEFAULT_WATCH_SERIES = (
    "KXBTC",
    "KXETH",
    "KXSOLE",
    "KXXRP",
    "KXDOGE",
    "KXTEMPNYCH",
    "KXTEMPCHI",
    "KXTEMPMIA",
    "KXTEMPAUS",
    "KXTEMPLAX",
)


def discover_quoted_market_tickers(
    *,
    client: KalshiClient,
    series: Sequence[str],
    max_markets_per_series: int,
    max_quoted_per_series: int,
    preferred_tickers: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Discover bounded open books without writing local or exchange state."""

    rows: list[dict[str, Any]] = []
    selected_tickers: set[str] = set()
    for ticker in dict.fromkeys(
        str(item).strip() for item in preferred_tickers if str(item).strip()
    ):
        try:
            orderbook = client.get_orderbook(ticker)
        except Exception:
            continue
        yes_levels, no_levels = _book_levels(orderbook)
        if not yes_levels and not no_levels:
            continue
        rows.append(
            {
                "ticker": ticker,
                "series_ticker": _series_for_ticker(ticker, series),
                "yes_levels": len(yes_levels),
                "no_levels": len(no_levels),
                "selection_source": "ACTIONABLE_RANKING",
            }
        )
        selected_tickers.add(ticker)

    for series_ticker in dict.fromkeys(str(item).strip() for item in series if item):
        already_selected = sum(1 for row in rows if row.get("series_ticker") == series_ticker)
        if already_selected >= max_quoted_per_series:
            continue
        payload = client.get_markets(
            status="open",
            limit=max_markets_per_series,
            series_ticker=series_ticker,
        )
        selected = already_selected
        for market in list(payload.get("markets") or [])[:max_markets_per_series]:
            ticker = str(market.get("ticker") or "").strip()
            if not ticker or ticker in selected_tickers:
                continue
            orderbook = client.get_orderbook(ticker)
            yes_levels, no_levels = _book_levels(orderbook)
            if not yes_levels and not no_levels:
                continue
            rows.append(
                {
                    "ticker": ticker,
                    "series_ticker": series_ticker,
                    "yes_levels": len(yes_levels),
                    "no_levels": len(no_levels),
                    "selection_source": "SERIES_FALLBACK",
                }
            )
            selected_tickers.add(ticker)
            selected += 1
            if selected >= max_quoted_per_series:
                break
    return rows


def load_actionable_tickers(path: Path, *, limit: int = 40) -> list[str]:
    """Load a bounded GH-2 candidate manifest without failing the watch."""

    if limit < 1:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return []
    raw_tickers = payload.get("tickers") if isinstance(payload, dict) else None
    if not isinstance(raw_tickers, list):
        return []
    return list(
        dict.fromkeys(str(ticker).strip() for ticker in raw_tickers if str(ticker).strip())
    )[:limit]


def run_reconnecting_websocket_watch(
    *,
    settings: Settings,
    series: Sequence[str] = DEFAULT_WATCH_SERIES,
    max_markets_per_series: int = 30,
    max_quoted_per_series: int = 2,
    stream_max_seconds: float = 60.0,
    discovery_refresh_seconds: float = 900.0,
    healthy_cycle_delay_seconds: float = 2.0,
    reconnect_initial_seconds: float = 5.0,
    reconnect_max_seconds: float = 120.0,
    persist_every_deltas: int = 25,
    status_path: Path = Path("reports/phase_gh1/watch/status.json"),
    preferred_tickers_path: Path | None = None,
    max_preferred_tickers: int = 40,
    max_cycles: int | None = None,
    client_factory: Callable[..., Any] = KalshiClient,
    adapter_factory: Callable[..., Any] = adapter_from_settings,
    discovery_fn: Callable[..., list[dict[str, Any]]] = discover_quoted_market_tickers,
    async_runner: Callable[[Any], StreamSummary] = asyncio.run,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run a reconnecting, filesystem-stage-only orderbook producer."""

    if not settings.kalshi_websocket_enabled:
        raise RuntimeError(
            "GH-1 WebSocket ingestion is disabled by KALSHI_WEBSOCKET_ENABLED=false."
        )
    if not series:
        raise ValueError("At least one exact series ticker is required.")
    if (
        max_markets_per_series < 1
        or max_quoted_per_series < 1
        or stream_max_seconds <= 0
        or discovery_refresh_seconds <= 0
        or healthy_cycle_delay_seconds < 0
        or reconnect_initial_seconds <= 0
        or reconnect_max_seconds < reconnect_initial_seconds
        or persist_every_deltas < 1
        or max_preferred_tickers < 1
        or (max_cycles is not None and max_cycles < 1)
    ):
        raise ValueError("GH-1 watch bounds and reconnect intervals are invalid.")

    started_at = utc_now()
    cached_tickers: list[str] = []
    next_discovery_at = 0.0
    backoff_seconds = reconnect_initial_seconds
    cycles_started = 0
    stream_cycles_completed = 0
    reconnect_count = 0
    discovery_refreshes = 0
    discovery_failures = 0
    messages_seen = 0
    snapshots_seen = 0
    deltas_applied = 0
    sequence_recoveries = 0
    staged_files = 0
    recent_errors: list[str] = []
    preferred_tickers: list[str] = []
    preferred_selected = 0
    state = "STARTING"
    next_retry_seconds: float | None = None
    consecutive_failures = 0
    last_discovery_success_at: str | None = None
    last_stream_success_at: str | None = None
    last_message_at: str | None = None
    last_snapshot_at: str | None = None

    def status_payload() -> dict[str, Any]:
        return {
            "phase": "GH-1-WATCH",
            "generated_at": utc_now().isoformat(),
            "started_at": started_at.isoformat(),
            "state": state,
            "series": list(series),
            "selected_tickers": list(cached_tickers),
            "preferred_tickers_path": (
                str(preferred_tickers_path) if preferred_tickers_path else None
            ),
            "preferred_tickers_loaded": len(preferred_tickers),
            "preferred_tickers_selected": preferred_selected,
            "cycles_started": cycles_started,
            "stream_cycles_completed": stream_cycles_completed,
            "reconnect_count": reconnect_count,
            "discovery_refreshes": discovery_refreshes,
            "discovery_failures": discovery_failures,
            "messages_seen": messages_seen,
            "snapshots_seen": snapshots_seen,
            "deltas_applied": deltas_applied,
            "sequence_recoveries": sequence_recoveries,
            "staged_files": staged_files,
            "next_retry_seconds": next_retry_seconds,
            "consecutive_failures": consecutive_failures,
            "last_discovery_success_at": last_discovery_success_at,
            "last_stream_success_at": last_stream_success_at,
            "last_message_at": last_message_at,
            "last_snapshot_at": last_snapshot_at,
            "recent_errors": recent_errors[-20:],
            "staging_dir": settings.kalshi_websocket_staging_dir,
            "safety": {
                "filesystem_stage_only": True,
                "database_writes": 0,
                "orders_submitted": 0,
                "execution_enabled": False,
                "autopilot_enabled": False,
            },
        }

    def write_status() -> None:
        _atomic_write_json(status_path, status_payload())

    write_status()
    try:
        while max_cycles is None or cycles_started < max_cycles:
            cycles_started += 1
            state = "CONNECTING"
            next_retry_seconds = None
            write_status()
            delay_seconds = healthy_cycle_delay_seconds
            try:
                with client_factory(settings=settings) as client:
                    now_monotonic = monotonic_fn()
                    if not cached_tickers or now_monotonic >= next_discovery_at:
                        state = "DISCOVERING_QUOTED_BOOKS"
                        write_status()
                        try:
                            preferred_tickers = (
                                load_actionable_tickers(
                                    preferred_tickers_path,
                                    limit=max_preferred_tickers,
                                )
                                if preferred_tickers_path is not None
                                else []
                            )
                            rows = discovery_fn(
                                client=client,
                                series=series,
                                max_markets_per_series=max_markets_per_series,
                                max_quoted_per_series=max_quoted_per_series,
                                preferred_tickers=preferred_tickers,
                            )
                        except Exception as exc:
                            discovery_failures += 1
                            recent_errors.append(f"discovery: {exc}")
                            next_discovery_at = now_monotonic + min(60.0, discovery_refresh_seconds)
                            if not cached_tickers:
                                raise
                        else:
                            discovered = list(
                                dict.fromkeys(
                                    str(row.get("ticker") or "").strip()
                                    for row in rows
                                    if row.get("ticker")
                                )
                            )
                            if discovered:
                                cached_tickers = discovered
                                preferred_selected = sum(
                                    1
                                    for row in rows
                                    if row.get("selection_source") == "ACTIONABLE_RANKING"
                                )
                                discovery_refreshes += 1
                                last_discovery_success_at = utc_now().isoformat()
                                next_discovery_at = now_monotonic + discovery_refresh_seconds
                            elif not cached_tickers:
                                raise RuntimeError(
                                    "No visibly quoted books were discovered for the "
                                    "configured series."
                                )
                    state = "STREAMING"
                    write_status()
                    adapter = adapter_factory(
                        settings=settings,
                        tickers=cached_tickers,
                        rest_client=client,
                        persist_every_deltas=persist_every_deltas,
                    )
                    summary = async_runner(
                        adapter.run(max_messages=None, max_seconds=stream_max_seconds)
                    )
                stream_cycles_completed += 1
                messages_seen += summary.messages_seen
                snapshots_seen += summary.snapshots_seen
                deltas_applied += summary.deltas_applied
                sequence_recoveries += summary.sequence_recoveries
                staged_files += len(summary.staged_files)
                recent_errors.extend(str(error) for error in summary.errors)
                completed_at = utc_now().isoformat()
                if summary.messages_seen > 0:
                    last_message_at = completed_at
                if summary.snapshots_seen > 0:
                    last_snapshot_at = completed_at
                if summary.timed_out or summary.messages_seen > 0 or summary.snapshots_seen > 0:
                    last_stream_success_at = completed_at
                if summary.timed_out:
                    state = "STREAM_CYCLE_COMPLETE"
                    backoff_seconds = reconnect_initial_seconds
                    consecutive_failures = 0
                else:
                    state = "RECONNECT_BACKOFF"
                    reconnect_count += 1
                    consecutive_failures += 1
                    delay_seconds = backoff_seconds
                    next_retry_seconds = delay_seconds
                    backoff_seconds = min(backoff_seconds * 2, reconnect_max_seconds)
            except Exception as exc:
                reconnect_count += 1
                consecutive_failures += 1
                recent_errors.append(f"stream: {exc}")
                state = "RECONNECT_BACKOFF"
                delay_seconds = backoff_seconds
                next_retry_seconds = delay_seconds
                backoff_seconds = min(backoff_seconds * 2, reconnect_max_seconds)

            if max_cycles is not None and cycles_started >= max_cycles:
                state = "STOPPED_MAX_CYCLES"
                next_retry_seconds = None
                write_status()
                break
            write_status()
            if delay_seconds > 0:
                sleep_fn(delay_seconds)
    except KeyboardInterrupt:
        state = "STOPPED_INTERRUPTED"
        next_retry_seconds = None
        write_status()

    return status_payload()


def _book_levels(payload: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    container = payload.get("orderbook_fp") or payload.get("orderbook") or payload
    if not isinstance(container, dict):
        return [], []
    yes = container.get("yes_dollars") or container.get("yes") or []
    no = container.get("no_dollars") or container.get("no") or []
    return list(yes) if isinstance(yes, list) else [], list(no) if isinstance(no, list) else []


def _series_for_ticker(ticker: str, series: Sequence[str]) -> str | None:
    matches = [str(item) for item in series if ticker.startswith(str(item))]
    return max(matches, key=len) if matches else None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
