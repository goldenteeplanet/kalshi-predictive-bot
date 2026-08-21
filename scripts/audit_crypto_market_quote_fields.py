#!/usr/bin/env python3
"""Compare live top-level Kalshi quotes with sibling REST orderbooks."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select

from kalshi_predictor.data.db import session_scope
from kalshi_predictor.data.schema import CryptoCurrentEvent
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.kalshi.orderbook import parse_orderbook


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--events", type=int, default=5)
    parser.add_argument("--max-workers", type=int, default=32)
    args = parser.parse_args()

    with session_scope() as session:
        registry = list(
            session.scalars(
                select(CryptoCurrentEvent)
                .order_by(
                    CryptoCurrentEvent.refreshed_at.desc(),
                    CryptoCurrentEvent.close_time.desc(),
                )
                .limit(args.events)
            )
        )

    event_rows = []
    # This audit compares representations rather than measuring coherence, so
    # retain the normal public-API throttle to avoid bias from 429 responses.
    with KalshiClient() as client:
        for registry_event in registry:
            page = client.get_markets(
                status=None, limit=1000, event_ticker=registry_event.event_ticker
            )
            markets = [row for row in page.get("markets", []) if isinstance(row, dict)]
            books = _fetch_books(client, markets, max_workers=args.max_workers)
            buckets = [
                audit_bucket(market, books.get(str(market.get("ticker"))))
                for market in markets
            ]
            counts = _counts(buckets)
            statuses = sorted({str(row.get("status") or "unknown") for row in markets})
            event_rows.append(
                {
                    "event_ticker": registry_event.event_ticker,
                    "series_ticker": registry_event.series_ticker,
                    "registry_close_time": registry_event.close_time.isoformat(),
                    "market_statuses": statuses,
                    "bucket_count": len(buckets),
                    "classification_counts": counts,
                    "all_buckets_executable_from_book": counts.get("BOOK_EXECUTABLE", 0)
                    == len(buckets),
                    "all_buckets_executable_with_top_level_fallback": (
                        counts.get("BOOK_EXECUTABLE", 0)
                        + counts.get("TOP_LEVEL_EXECUTABLE_ONLY", 0)
                        == len(buckets)
                    ),
                    "buckets": buckets,
                }
            )

    open_events = [
        row
        for row in event_rows
        if any(status in {"open", "active"} for status in row["market_statuses"])
    ]
    top_level_only = sum(
        row["classification_counts"].get("TOP_LEVEL_EXECUTABLE_ONLY", 0)
        for row in open_events
    )
    genuinely_unquoted = sum(
        row["classification_counts"].get("GENUINELY_UNQUOTED", 0) for row in open_events
    )
    complete_with_fallback = sum(
        bool(row["all_buckets_executable_with_top_level_fallback"]) for row in open_events
    )
    if top_level_only and complete_with_fallback:
        decision = "ALLOW_LIVE_TOP_LEVEL_EXECUTABLE_FALLBACK"
    else:
        decision = "EXCLUDE_SPARSE_BOOK_EVENTS"
    payload = {
        "policy": "LIVE_SAME_RUN_TOP_LEVEL_VS_ORDERBOOK_AUDIT",
        "events_requested": args.events,
        "events_audited": len(event_rows),
        "open_events_audited": len(open_events),
        "open_top_level_executable_only_buckets": top_level_only,
        "open_genuinely_unquoted_buckets": genuinely_unquoted,
        "open_events_complete_with_top_level_fallback": complete_with_fallback,
        "decision": decision,
        "events": event_rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "events"}))
    return 0


def audit_bucket(market: dict[str, Any], orderbook: dict[str, Any] | None) -> dict[str, Any]:
    book = parse_orderbook(orderbook)
    book_bid = book.best_yes_bid
    book_ask = book.best_yes_ask
    top_bid = _quote(market, "yes_bid_dollars", "yes_bid")
    top_ask = _quote(market, "yes_ask_dollars", "yes_ask")
    if book_bid is not None and book_ask is not None:
        classification = "BOOK_EXECUTABLE"
    elif top_bid is not None and top_ask is not None:
        classification = "TOP_LEVEL_EXECUTABLE_ONLY"
    elif all(value is None for value in (book_bid, book_ask, top_bid, top_ask)):
        classification = "GENUINELY_UNQUOTED"
    else:
        classification = "PARTIAL_QUOTE"
    return {
        "ticker": market.get("ticker"),
        "status": market.get("status"),
        "classification": classification,
        "book_yes_bid": _string(book_bid),
        "book_yes_ask": _string(book_ask),
        "top_yes_bid": _string(top_bid),
        "top_yes_ask": _string(top_ask),
        "raw_top_level_fields": {
            key: market.get(key)
            for key in (
                "yes_bid_dollars",
                "yes_ask_dollars",
                "no_bid_dollars",
                "no_ask_dollars",
                "yes_bid",
                "yes_ask",
                "no_bid",
                "no_ask",
            )
            if key in market
        },
        "orderbook_present": orderbook is not None,
    }


def _fetch_books(
    client: KalshiClient, markets: list[dict[str, Any]], *, max_workers: int
) -> dict[str, dict[str, Any] | None]:
    results: dict[str, dict[str, Any] | None] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(markets)))) as executor:
        futures = {
            executor.submit(client.get_orderbook, str(row["ticker"])): str(row["ticker"])
            for row in markets
            if row.get("ticker")
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results[ticker] = future.result()
            except Exception:
                results[ticker] = None
    return results


def _quote(market: dict[str, Any], dollars_key: str, cents_key: str) -> Decimal | None:
    dollars = _decimal(market.get(dollars_key))
    if dollars is not None:
        return dollars if Decimal("0") < dollars < Decimal("1") else None
    cents = _decimal(market.get(cents_key))
    if cents is None:
        return None
    normalized = cents / Decimal("100")
    return normalized if Decimal("0") < normalized < Decimal("1") else None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        classification = row["classification"]
        counts[classification] = counts.get(classification, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
