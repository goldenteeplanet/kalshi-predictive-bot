"""Atomic, event-coherent quote collection for mutually exclusive crypto buckets."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.crypto.linker import link_crypto_markets
from kalshi_predictor.data.repositories import encode_json, insert_market_snapshot
from kalshi_predictor.data.schema import (
    CryptoCurrentEvent,
    CryptoEventLiquidityCoverage,
    CryptoEventQuoteCapture,
    Forecast,
    Market,
    MarketSnapshot,
    Settlement,
)
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.kalshi.orderbook import parse_orderbook
from kalshi_predictor.research.liquidity_priority import family_key, family_yield_score
from kalshi_predictor.research.liquidity_window import in_recommended_window
from kalshi_predictor.utils.time import utc_now

SERIES_SYMBOLS = {
    "KXBTC": "BTC",
    "KXETH": "ETH",
    "KXSOLE": "SOL",
    "KXXRP": "XRP",
    "KXDOGE": "DOGE",
}


class PublicKalshiClient(Protocol):
    def get_markets(self, **kwargs: Any) -> dict[str, Any]: ...
    def iter_markets(self, **kwargs: Any) -> Any: ...
    def get_orderbook(self, ticker: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class EventCandidate:
    event_ticker: str
    series_ticker: str
    symbol: str
    markets: tuple[dict[str, Any], ...]


def select_candidates_for_liquidity_window(
    candidates: list[EventCandidate],
    policy: dict[str, Any],
    *,
    now: datetime,
    fallback_when_empty: bool = True,
) -> list[EventCandidate]:
    recommendation = policy.get("recommended_window") or {}
    if not policy.get("scheduling_enabled") or not recommendation:
        return candidates
    selected = []
    for candidate in candidates:
        close_times = [
            parsed
            for market in candidate.markets
            if (parsed := _datetime(market.get("close_time"))) is not None
        ]
        if close_times and in_recommended_window(max(close_times), now, recommendation):
            selected.append(candidate)
    # Do not halt collection when the registry has no event in the learned
    # window; retain the yield-ranked fallback until more timing data exists.
    return selected or (candidates if fallback_when_empty else [])


def refresh_targeted_event_forecasts(
    session: Session,
    client: PublicKalshiClient,
    candidates: list[EventCandidate],
    *,
    max_events: int,
    max_bucket_probes: int = 8,
    capture_immediately: bool = False,
    capture_latency_budget_seconds: float = 30.0,
    capture_coherence_ms: int = 2500,
    capture_max_workers: int = 16,
    capture_bucket_request_budget: int = 150,
) -> dict[str, Any]:
    rows = []
    capture_buckets_admitted = 0
    for candidate in candidates[: max(0, max_events)]:
        family = family_key(candidate.series_ticker, len(candidate.markets))
        buckets, reasons = validate_topology(list(candidate.markets))
        if reasons:
            rows.append(
                {
                    "event_ticker": candidate.event_ticker,
                    "family": family,
                    "status": "SKIPPED",
                    "buckets_probed": 0,
                    "midpoint_probe_failures": 0,
                    "reasons": reasons,
                }
            )
            continue
        if (
            capture_immediately
            and capture_buckets_admitted + len(buckets) > capture_bucket_request_budget
        ):
            rows.append(
                {
                    "event_ticker": candidate.event_ticker,
                    "family": family,
                    "status": "SKIPPED",
                    "buckets_probed": 0,
                    "midpoint_probe_failures": 0,
                    "reasons": ["CAPTURE_REQUEST_BUDGET_EXCEEDED_BEFORE_FORECAST"],
                }
            )
            continue
        selected: tuple[dict[str, Any], dict[str, Any]] | None = None
        probe_failures = []
        buckets_probed = 0
        midpoint_probe_failures = 0
        one_sided_bound_uses = 0
        for representative in _representative_buckets(buckets)[:max_bucket_probes]:
            market = representative["market"]
            ticker = str(market["ticker"])
            buckets_probed += 1
            try:
                orderbook = client.get_orderbook(ticker)
            except Exception as exc:
                probe_failures.append(f"ORDERBOOK_PROBE_FAILED:{ticker}:{type(exc).__name__}")
                continue
            prices = parse_orderbook(orderbook)
            if prices.best_yes_bid is not None and prices.best_yes_ask is not None:
                selected = (market, orderbook)
                break
            midpoint_probe_failures += 1
            if prices.best_yes_bid is not None or prices.best_yes_ask is not None:
                one_sided_bound_uses = 1
                selected = (market, orderbook)
                break
        if selected is None:
            rows.append(
                {
                    "event_ticker": candidate.event_ticker,
                    "family": family,
                    "status": "SKIPPED",
                    "buckets_probed": buckets_probed,
                    "midpoint_probe_failures": midpoint_probe_failures,
                    "one_sided_bound_uses": one_sided_bound_uses,
                    "reasons": [*probe_failures, "NO_FORECASTABLE_EXECUTABLE_BOUND"],
                }
            )
            continue
        market, orderbook = selected
        ticker = str(market["ticker"])
        try:
            with session.begin_nested():
                forecast_timestamp = utc_now()
                snapshot = insert_market_snapshot(
                    session, market, orderbook, forecast_timestamp
                )
                session.flush()
                link_crypto_markets(session, tickers=[ticker], limit=1)
                summary = run_forecast_models(
                    session, model_name="crypto_v2", snapshots=[snapshot]
                )
            result_row = {
                "event_ticker": candidate.event_ticker,
                "family": family,
                "ticker": ticker,
                "snapshot_id": snapshot.id,
                "forecast_timestamp": forecast_timestamp.isoformat(),
                "status": "FORECASTED" if summary.forecasts_inserted else "SKIPPED",
                "buckets_probed": buckets_probed,
                "midpoint_probe_failures": midpoint_probe_failures,
                "one_sided_bound_uses": one_sided_bound_uses,
                "forecasts_inserted": summary.forecasts_inserted,
                "forecasts_skipped": summary.skipped,
                "reasons": (
                    []
                    if summary.forecasts_inserted
                    else ["CRYPTO_V2_FORECAST_SKIPPED"]
                ),
            }
            if summary.forecasts_inserted and capture_immediately:
                capture_buckets_admitted += len(buckets)
                result_row["immediate_capture"] = _capture_immediate_polytope(
                    session,
                    client,
                    candidate,
                    forecast_timestamp=forecast_timestamp,
                    latency_budget_seconds=capture_latency_budget_seconds,
                    coherence_limit_ms=capture_coherence_ms,
                    max_workers=capture_max_workers,
                )
            rows.append(result_row)
        except Exception as exc:  # preserve per-event failure without losing the batch
            rows.append(
                {
                    "event_ticker": candidate.event_ticker,
                    "family": family,
                    "ticker": ticker,
                    "status": "FAILED",
                    "buckets_probed": buckets_probed,
                    "midpoint_probe_failures": midpoint_probe_failures,
                    "one_sided_bound_uses": one_sided_bound_uses,
                    "reasons": [f"TARGETED_FORECAST_FAILED:{type(exc).__name__}"],
                }
            )
    return {
        "policy": "RANKED_IN_WINDOW_FORECAST_IMMEDIATELY_BEFORE_SIBLING_CAPTURE",
        "events_attempted": len(rows),
        "events_forecasted": sum(row["status"] == "FORECASTED" for row in rows),
        "midpoint_probe_failures": sum(
            int(row.get("midpoint_probe_failures", 0)) for row in rows
        ),
        "one_sided_bound_uses": sum(
            int(row.get("one_sided_bound_uses", 0)) for row in rows
        ),
        "immediate_captures_attempted": sum(
            bool(row.get("immediate_capture", {}).get("attempted")) for row in rows
        ),
        "immediate_captures_within_budget": sum(
            bool(row.get("immediate_capture", {}).get("within_latency_budget"))
            for row in rows
        ),
        "capture_bucket_request_budget": capture_bucket_request_budget,
        "capture_buckets_admitted": capture_buckets_admitted,
        "rows": rows,
    }


def _capture_immediate_polytope(
    session: Session,
    client: PublicKalshiClient,
    candidate: EventCandidate,
    *,
    forecast_timestamp: datetime,
    latency_budget_seconds: float,
    coherence_limit_ms: int,
    max_workers: int,
) -> dict[str, Any]:
    capture_started_at = utc_now()
    deadline = time.monotonic() + max(0.0, latency_budget_seconds)
    capture = None
    reasons: list[str] = []
    attempts = 0
    coverage = None
    while attempts < 2 and time.monotonic() <= deadline:
        attempts += 1
        capture, reasons = capture_candidate(
            session,
            client,
            candidate,
            coherence_limit_ms=coherence_limit_ms,
            max_workers=max_workers,
        )
        coverage = session.scalar(
            select(CryptoEventLiquidityCoverage)
            .where(
                CryptoEventLiquidityCoverage.event_ticker == candidate.event_ticker,
                CryptoEventLiquidityCoverage.captured_at >= forecast_timestamp,
            )
            .order_by(CryptoEventLiquidityCoverage.captured_at.desc())
            .limit(1)
        )
        if coverage is not None:
            break
        remaining = deadline - time.monotonic()
        if attempts < 2 and remaining > 0:
            time.sleep(min(1.0, remaining))
    capture_completed_at = utc_now()
    coverage_timestamp = coverage.captured_at if coverage is not None else None
    if (
        coverage_timestamp is not None
        and coverage_timestamp.tzinfo is None
        and forecast_timestamp.tzinfo is not None
    ):
        coverage_timestamp = coverage_timestamp.replace(tzinfo=forecast_timestamp.tzinfo)
    latency_seconds = (
        (coverage_timestamp - forecast_timestamp).total_seconds()
        if coverage_timestamp is not None
        else (capture_completed_at - forecast_timestamp).total_seconds()
    )
    within_budget = (
        coverage is not None
        and 0.0 <= latency_seconds <= max(0.0, latency_budget_seconds)
    )
    return {
        "attempted": True,
        "attempt_count": attempts,
        "capture_started_at": capture_started_at.isoformat(),
        "capture_completed_at": capture_completed_at.isoformat(),
        "coverage_id": coverage.id if coverage is not None else None,
        "complete_vector_capture_id": capture.id if capture is not None else None,
        "forecast_to_capture_latency_seconds": latency_seconds,
        "latency_budget_seconds": latency_budget_seconds,
        "within_latency_budget": within_budget,
        "status": (
            "EVALUABLE_POLYTOPE_WITHIN_BUDGET"
            if within_budget
            else "CAPTURE_MISSING_OR_LATENCY_BUDGET_EXCEEDED"
        ),
        "reasons": reasons,
    }


def _representative_buckets(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    interiors = [row for row in buckets if row["kind"] == "interior"]
    rows = interiors or buckets
    center = (len(rows) - 1) / 2.0
    return [
        row
        for _, row in sorted(
            enumerate(rows), key=lambda item: (abs(item[0] - center), item[0])
        )
    ]


def select_candidates_with_fresh_forecasts(
    session: Session,
    candidates: list[EventCandidate],
    *,
    now: datetime,
    model_name: str = "crypto_v2",
    max_lag_minutes: int = 30,
) -> list[EventCandidate]:
    if not candidates:
        return []
    events = [candidate.event_ticker for candidate in candidates]
    rows = session.execute(
        select(Market.event_ticker, func.max(Forecast.forecasted_at))
        .join(Forecast, Forecast.ticker == Market.ticker)
        .where(
            Market.event_ticker.in_(events),
            Forecast.model_name == model_name,
            Forecast.forecasted_at <= now,
        )
        .group_by(Market.event_ticker)
    ).all()
    latest = {str(event): captured for event, captured in rows if event and captured}
    selected = []
    for candidate in candidates:
        forecasted_at = latest.get(candidate.event_ticker)
        if forecasted_at is None:
            continue
        if forecasted_at.tzinfo is None and now.tzinfo is not None:
            forecasted_at = forecasted_at.replace(tzinfo=now.tzinfo)
        lag = (now - forecasted_at).total_seconds() / 60.0
        if 0.0 <= lag <= max_lag_minutes:
            selected.append(candidate)
    return selected


def bucket_interval(market: dict[str, Any]) -> dict[str, Any] | None:
    strike_type = str(market.get("strike_type") or "").lower()
    floor = _number(market.get("floor_strike"))
    cap = _number(market.get("cap_strike"))
    if strike_type == "between" and floor is not None and cap is not None and floor < cap:
        return {"kind": "interior", "lower": floor, "upper": cap}
    if strike_type == "less" and cap is not None:
        return {"kind": "lower_tail", "lower": None, "upper": cap}
    if strike_type == "greater" and floor is not None:
        return {"kind": "upper_tail", "lower": floor, "upper": None}
    return None


def validate_topology(markets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    buckets: list[dict[str, Any]] = []
    reasons: list[str] = []
    for market in markets:
        interval = bucket_interval(market)
        if interval is None:
            reasons.append(f"UNPARSEABLE_BUCKET:{market.get('ticker', '?')}")
        else:
            buckets.append({"market": market, **interval})
    buckets.sort(key=lambda row: float("-inf") if row["lower"] is None else row["lower"])
    if not buckets or buckets[0]["kind"] != "lower_tail":
        reasons.append("LOWER_TAIL_MISSING")
    if not buckets or buckets[-1]["kind"] != "upper_tail":
        reasons.append("UPPER_TAIL_MISSING")
    if len(buckets) < 3 or not any(row["kind"] == "interior" for row in buckets):
        reasons.append("INTERIOR_BUCKET_MISSING")
    for left, right in zip(buckets, buckets[1:], strict=False):
        if left["upper"] is None or right["lower"] is None:
            reasons.append("INVALID_TAIL_ORDER")
        elif abs(left["upper"] - right["lower"]) > 0.011:
            reasons.append("BUCKET_GAP_OR_OVERLAP")
    tickers = [str(row["market"].get("ticker") or "") for row in buckets]
    if not all(tickers) or len(tickers) != len(set(tickers)):
        reasons.append("INVALID_OR_DUPLICATE_TICKER")
    return buckets, sorted(set(reasons))


def discover_candidates(
    client: PublicKalshiClient, series: list[str], *, limit: int = 100
) -> list[EventCandidate]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(len(series), 5)) as executor:
        futures = {
            executor.submit(
                client.get_markets,
                status="open",
                limit=min(limit, 1000),
                series_ticker=series_ticker,
            ): series_ticker
            for series_ticker in series
        }
        pages = [(futures[future], future.result()) for future in as_completed(futures)]
    for series_ticker, page in pages:
        for market in page.get("markets", []):
            event_ticker = str(market.get("event_ticker") or "")
            if event_ticker:
                grouped.setdefault((series_ticker, event_ticker), []).append(market)
    return [
        EventCandidate(event, series_ticker, SERIES_SYMBOLS[series_ticker], tuple(markets))
        for (series_ticker, event), markets in sorted(grouped.items())
        if series_ticker in SERIES_SYMBOLS
    ]


def discover_candidates_from_session(
    session: Session, series: list[str]
) -> list[EventCandidate]:
    """Use the preceding market-refresh transaction as the discovery watermark."""
    now = utc_now()
    prefix_checks = [Market.event_ticker.like(f"{value}%") for value in series]
    markets = session.scalars(
        select(Market).where(
            Market.status.in_(("open", "active")),
            Market.close_time >= now,
            Market.close_time <= now + timedelta(days=1),
            or_(Market.series_ticker.in_(series), *prefix_checks),
        )
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for market in markets:
        if not market.event_ticker:
            continue
        series_ticker = market.series_ticker
        if series_ticker not in SERIES_SYMBOLS:
            series_ticker = next(
                (value for value in series if market.event_ticker.startswith(value)), None
            )
        if series_ticker not in SERIES_SYMBOLS:
            continue
        raw = json.loads(market.raw_json or "{}")
        grouped.setdefault((series_ticker, market.event_ticker), []).append(raw)
    return [
        EventCandidate(event, series_ticker, SERIES_SYMBOLS[series_ticker], tuple(rows))
        for (series_ticker, event), rows in sorted(grouped.items())
        if series_ticker in SERIES_SYMBOLS
    ]


def discover_candidates_from_registry(
    session: Session, series: list[str]
) -> list[EventCandidate]:
    rows = session.scalars(
        select(CryptoCurrentEvent)
        .where(
            CryptoCurrentEvent.series_ticker.in_(series),
            CryptoCurrentEvent.close_time >= utc_now(),
        )
        .order_by(CryptoCurrentEvent.close_time, CryptoCurrentEvent.event_ticker)
    )
    candidates = [
        EventCandidate(
            row.event_ticker,
            row.series_ticker,
            SERIES_SYMBOLS[row.series_ticker],
            tuple(json.loads(row.markets_json)),
        )
        for row in rows
    ]
    latest: dict[str, CryptoEventLiquidityCoverage] = {}
    coverage_rows = session.scalars(
        select(CryptoEventLiquidityCoverage).order_by(
            CryptoEventLiquidityCoverage.captured_at.desc()
        )
    )
    for row in coverage_rows:
        latest.setdefault(row.event_ticker, row)
    family_rows: dict[str, list[CryptoEventLiquidityCoverage]] = {}
    for row in latest.values():
        family_rows.setdefault(row.family, []).append(row)

    def priority(item: EventCandidate) -> tuple[float | int | str, ...]:
        family = family_key(item.series_ticker, len(item.markets))
        observations = family_rows.get(family, [])
        count = len(observations)
        average = (
            sum(float(row.two_sided_coverage) for row in observations) / count
            if count
            else 0.50
        )
        score = family_yield_score(
            average_coverage=average,
            bounds_failure_rate=(
                sum(row.bounds_feasible != "true" for row in observations) / count
                if count
                else 0.0
            ),
            coherence_failure_rate=(
                sum(row.coherence_ms > 2500 for row in observations) / count
                if count
                else 0.0
            ),
            bucket_count=len(item.markets),
            observed_events=count,
        )
        event_was_measured = item.event_ticker in latest
        return (-score, int(event_was_measured), len(item.markets), item.event_ticker)

    return sorted(candidates, key=priority)


def backfill_registry(
    session: Session,
    client: PublicKalshiClient,
    *,
    source_watermark: datetime,
    series: list[str],
) -> dict[str, Any]:
    latest_snapshot = session.scalar(select(func.max(MarketSnapshot.captured_at)))
    if latest_snapshot is not None and latest_snapshot.tzinfo is None:
        latest_snapshot = latest_snapshot.replace(tzinfo=source_watermark.tzinfo)
    effective_watermark = source_watermark
    if latest_snapshot is not None:
        effective_watermark = max(source_watermark, latest_snapshot - timedelta(minutes=15))
    seed_rows = session.execute(
        select(Market.event_ticker)
        .join(MarketSnapshot, MarketSnapshot.ticker == Market.ticker)
        .where(
            MarketSnapshot.captured_at >= effective_watermark,
            Market.event_ticker.is_not(None),
            Market.close_time >= utc_now(),
        )
        .distinct()
    ).all()
    events = sorted({str(event) for (event,) in seed_rows if event})
    updated = 0
    rejected: dict[str, list[str]] = {}
    now = utc_now()
    pages: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(events)))) as executor:
        futures = {
            executor.submit(
                client.get_markets,
                status="open",
                limit=1000,
                event_ticker=event_ticker,
            ): event_ticker
            for event_ticker in events
        }
        for future in as_completed(futures):
            event_ticker = futures[future]
            try:
                pages[event_ticker] = future.result()
            except Exception as exc:
                rejected[event_ticker] = [f"EVENT_EXPANSION_FAILED:{type(exc).__name__}"]
    for event_ticker, page in sorted(pages.items()):
        inferred = next((value for value in series if event_ticker.startswith(value)), None)
        if inferred is None:
            rejected[event_ticker] = ["SERIES_NOT_INFERABLE"]
            continue
        markets = [row for row in page.get("markets", []) if isinstance(row, dict)]
        buckets, reasons = validate_topology(markets)
        close_times = [
            value
            for row in markets
            if (value := _datetime(row.get("close_time"))) is not None
        ]
        if not close_times:
            reasons.append("CLOSE_TIME_MISSING")
        if reasons:
            rejected[event_ticker] = sorted(set(reasons))
            continue
        close_time = max(close_times)
        existing = session.scalar(
            select(CryptoCurrentEvent).where(
                CryptoCurrentEvent.series_ticker == inferred,
                CryptoCurrentEvent.event_ticker == event_ticker,
                CryptoCurrentEvent.close_time == close_time,
            )
        )
        if existing is None:
            existing = CryptoCurrentEvent(
                series_ticker=inferred,
                event_ticker=event_ticker,
                close_time=close_time,
                refreshed_at=now,
                source_watermark=source_watermark,
                bucket_count=len(buckets),
                markets_json="[]",
            )
            session.add(existing)
        existing.refreshed_at = now
        existing.source_watermark = source_watermark
        existing.bucket_count = len(buckets)
        existing.markets_json = encode_json([row["market"] for row in buckets])
        updated += 1
        session.commit()
    return {
        "seed_events": len(events),
        "effective_watermark": effective_watermark.isoformat(),
        "registry_events_updated": updated,
        "registry_events_rejected": len(rejected),
        "registry_rejections": rejected,
    }


def capture_candidate(
    session: Session,
    client: PublicKalshiClient,
    candidate: EventCandidate,
    *,
    coherence_limit_ms: int = 2500,
    max_workers: int = 16,
) -> tuple[CryptoEventQuoteCapture | None, list[str]]:
    if session.scalar(
        select(CryptoEventQuoteCapture.id).where(
            CryptoEventQuoteCapture.event_ticker == candidate.event_ticker
        )
    ) is not None:
        return None, ["EVENT_ALREADY_CAPTURED"]
    buckets, reasons = validate_topology(list(candidate.markets))
    if reasons:
        return None, reasons

    started_at = utc_now()
    started_mono = time.monotonic()
    fetched: dict[str, tuple[dict[str, Any], datetime, float]] = {}
    workers = min(max_workers, len(buckets))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(client.get_orderbook, str(row["market"]["ticker"])): row
            for row in buckets
        }
        for future in as_completed(futures):
            ticker = str(futures[future]["market"]["ticker"])
            try:
                book = future.result()
            except Exception as exc:  # fail the whole vector, preserving the cause
                reasons.append(f"ORDERBOOK_FETCH_FAILED:{ticker}:{type(exc).__name__}")
                continue
            fetched[ticker] = (book, utc_now(), time.monotonic())
    if len(fetched) != len(buckets):
        return None, sorted(set(reasons or ["ORDERBOOK_VECTOR_INCOMPLETE"]))

    elapsed = [item[2] - started_mono for item in fetched.values()]
    coherence_ms = round((max(elapsed) - min(elapsed)) * 1000)

    vector = []
    for row in buckets:
        market = row["market"]
        ticker = str(market["ticker"])
        book, fetched_at, _ = fetched[ticker]
        prices = parse_orderbook(book)
        if prices.best_yes_bid is None or prices.best_yes_ask is None:
            reasons.append(f"NON_EXECUTABLE_BOOK:{ticker}")
        vector.append(
            {
                "ticker": ticker,
                "kind": row["kind"],
                "lower": row["lower"],
                "upper": row["upper"],
                "fetched_at": fetched_at.isoformat(),
                "yes_bid": str(prices.best_yes_bid) if prices.best_yes_bid is not None else None,
                "yes_ask": str(prices.best_yes_ask) if prices.best_yes_ask is not None else None,
                "no_bid": str(prices.best_no_bid) if prices.best_no_bid is not None else None,
                "no_ask": str(prices.best_no_ask) if prices.best_no_ask is not None else None,
            }
        )
    coverage = liquidity_coverage(candidate, vector, coherence_ms=coherence_ms)
    session.add(coverage)
    session.flush()
    if coherence_ms > coherence_limit_ms:
        return None, [f"COHERENCE_WINDOW_EXCEEDED:{coherence_ms}>{coherence_limit_ms}"]
    if reasons:
        return None, sorted(set(reasons))

    capture_time = max(item[1] for item in fetched.values())
    snapshot_ids = []
    for row in buckets:
        market = row["market"]
        ticker = str(market["ticker"])
        snapshot = insert_market_snapshot(session, market, fetched[ticker][0], capture_time)
        snapshot_ids.append(snapshot.id)
    manifest = CryptoEventQuoteCapture(
        event_ticker=candidate.event_ticker,
        series_ticker=candidate.series_ticker,
        symbol=candidate.symbol,
        captured_at=started_at,
        completed_at=capture_time,
        coherence_ms=coherence_ms,
        bucket_count=len(vector),
        snapshot_ids_json=encode_json(snapshot_ids),
        vector_json=encode_json(vector),
    )
    session.add(manifest)
    session.flush()
    return manifest, []


def liquidity_coverage(
    candidate: EventCandidate, vector: list[dict[str, Any]], *, coherence_ms: int
) -> CryptoEventLiquidityCoverage:
    two_sided = bid_only = ask_only = unquoted = 0
    bounds = []
    lower_sum = upper_sum = 0.0
    for row in vector:
        executable = executable_yes_bounds(row)
        bid = executable["bid"]
        ask = executable["ask"]
        if bid is not None and ask is not None:
            two_sided += 1
        elif bid is not None:
            bid_only += 1
        elif ask is not None:
            ask_only += 1
        else:
            unquoted += 1
        lower = bid if bid is not None else 0.0
        upper = ask if ask is not None else 1.0
        lower_sum += lower
        upper_sum += upper
        bounds.append(
            {
                "ticker": row["ticker"],
                "kind": row.get("kind"),
                "has_bid": bid is not None,
                "has_ask": ask is not None,
                "yes_lower_source": executable["bid_source"],
                "yes_upper_source": executable["ask_source"],
                "lower": lower,
                "upper": upper,
            }
        )
    count = len(vector)
    coverage = two_sided / count if count else 0.0
    feasible = lower_sum <= 1.0 + 1e-9 and upper_sum >= 1.0 - 1e-9
    family = family_key(candidate.series_ticker, count)
    return CryptoEventLiquidityCoverage(
        event_ticker=candidate.event_ticker,
        series_ticker=candidate.series_ticker,
        symbol=candidate.symbol,
        family=family,
        captured_at=utc_now(),
        coherence_ms=coherence_ms,
        bucket_count=count,
        two_sided_count=two_sided,
        bid_only_count=bid_only,
        ask_only_count=ask_only,
        unquoted_count=unquoted,
        two_sided_coverage=f"{coverage:.8f}",
        complete_executable="true" if two_sided == count and count > 0 else "false",
        bounds_feasible="true" if feasible else "false",
        bounds_json=encode_json(
            {"sum_lower": lower_sum, "sum_upper": upper_sum, "buckets": bounds}
        ),
    )


def executable_yes_bounds(row: dict[str, Any]) -> dict[str, Any]:
    """Convert native YES/NO bids into executable YES probability bounds.

    Kalshi REST books contain bids for each outcome. A NO bid at n is an
    executable YES ask at 1-n. The reported NO ask is derived from the YES bid,
    so it must not be counted as an independent source of lower-bound liquidity.
    """
    yes_bid = _number(row.get("yes_bid"))
    no_bid = _number(row.get("no_bid"))
    yes_ask = 1.0 - no_bid if no_bid is not None else None
    return {
        "bid": yes_bid,
        "ask": yes_ask,
        "bid_source": "YES_BID" if yes_bid is not None else "MISSING",
        "ask_source": "NO_BID_COMPLEMENT" if no_bid is not None else "MISSING",
    }


def cohort_status(session: Session, *, target: int = 100) -> dict[str, Any]:
    captures = list(session.scalars(select(CryptoEventQuoteCapture)))
    registry_events = session.scalar(select(func.count()).select_from(CryptoCurrentEvent)) or 0
    settled = 0
    for capture in captures:
        tickers = [row["ticker"] for row in json.loads(capture.vector_json)]
        results = dict(
            session.execute(
                select(Market.ticker, Settlement.result)
                .join(Settlement, Settlement.ticker == Market.ticker)
                .where(Market.ticker.in_(tickers))
            ).all()
        )
        all_settled = len(results) == len(tickers) and all(
            value in {"yes", "no"} for value in results.values()
        )
        if all_settled:
            settled += 1
    return {
        "target_unique_settled_events": target,
        "registry_events": registry_events,
        "complete_vector_events": len(captures),
        "settled_complete_vector_events": settled,
        "remaining_settled_events": max(0, target - settled),
        "target_met": settled >= target,
        "multiclass_comparison_allowed": settled >= target,
    }


def discovery_inventory(session: Session) -> dict[str, int]:
    rows = session.execute(select(Market.series_ticker, Market.status, Market.event_ticker)).all()
    counts: dict[str, int] = {}
    prefixes = tuple(SERIES_SYMBOLS)
    for series, status, event in rows:
        if (series in SERIES_SYMBOLS) or str(event or "").startswith(prefixes):
            key = f"{series or 'NULL'}|{status or 'NULL'}"
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
