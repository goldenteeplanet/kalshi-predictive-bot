"""Actual named baseline from original research receipts, without database emulation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from kalshi_predictor.forecasting.base import ForecastInput
from kalshi_predictor.forecasting.market_implied import MarketImpliedForecaster
from kalshi_predictor.ingest.public_market_discovery import event_markets
from kalshi_predictor.kalshi.orderbook import parse_orderbook, usable_bid_ask_book

BASELINE_MODULES = (
    "kalshi_predictor.crypto.named_research_baselines",
    "kalshi_predictor.forecasting.market_implied",
    "kalshi_predictor.forecasting.base",
    "kalshi_predictor.kalshi.orderbook",
    "kalshi_predictor.utils.decimals",
    "kalshi_predictor.ingest.public_market_discovery",
)


def select_exact_receipt(
    receipts: Sequence[dict[str, Any]],
    *,
    sha256: str,
    request_url: str,
) -> dict[str, Any]:
    """Content equality is not endpoint identity: bind both, reject ambiguity."""
    matches = [r for r in receipts if r.get("sha256") == sha256 and r.get("url") == request_url]
    if len(matches) != 1:
        raise ValueError("BASELINE_EXACT_REQUEST_RECEIPT_REQUIRED")
    return dict(matches[0])


def _aware(value: datetime) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _original(raw: bytes, digest: str) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 2_000_000:
        raise ValueError("BASELINE_ORIGINAL_BYTES_BUDGET")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("BASELINE_ORIGINAL_HASH_MISMATCH")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("BASELINE_ORIGINAL_OBJECT_REQUIRED")
    return parsed


def _quote_pair(bid: Any, ask: Any) -> str:
    values = []
    for value in (bid, ask):
        if value is None:
            values.append(None)
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return "INVALID"
        if not number.is_finite() or not 0 <= number <= 1:
            return "INVALID"
        values.append(number)
    bid, ask = values
    if bid is None and ask is None:
        return "MISSING"
    if bid is None or ask is None:
        return "ONE_SIDED"
    if bid > ask:
        return "CROSSED"
    if bid == 0 and ask == 1:
        return "FULL_RANGE"
    return "TWO_SIDED"


def baseline_quote_quality(market: dict, book: dict | None, source: str | None) -> dict:
    """Label evidence without changing the actual named-model probability.

    Eligibility concerns a prospective comparison subset only, never execution.
    Last-trade age cannot be inferred from an HTTP receipt timestamp.
    """
    prices = parse_orderbook(book)
    depth = usable_bid_ask_book(book, side="YES")
    positive_depth = (
        depth.bid_price == prices.best_yes_bid
        and depth.ask_price == prices.best_yes_ask
        and depth.bid_depth is not None
        and depth.ask_depth is not None
        and depth.bid_depth > 0
        and depth.ask_depth > 0
    )
    listing_state = _quote_pair(market.get("yes_bid_dollars"), market.get("yes_ask_dollars"))
    book_state = (
        _quote_pair(prices.best_yes_bid, prices.best_yes_ask)
        if book is not None
        else "NOT_CAPTURED"
    )
    if source == "orderbook_midpoint":
        label = "BOOK_" + book_state
        if book_state == "TWO_SIDED" and not positive_depth:
            label = "BOOK_TWO_SIDED_DEPTH_UNVERIFIED"
    elif source == "market_quote_midpoint":
        label = "LISTING_" + listing_state
    elif source == "last_price":
        label = "LAST_TRADE_AGE_UNVERIFIED"
    else:
        label = "NO_BASELINE_PROBABILITY"
    return {
        "schema": "baseline-quote-quality-v1",
        "label": label,
        "listing_state": listing_state,
        "book_state": book_state,
        "positive_depth_at_baseline_prices": positive_depth,
        "nonvacuous_midpoint_comparison_eligible": label in {"BOOK_TWO_SIDED", "LISTING_TWO_SIDED"},
        "book_midpoint_comparison_eligible": label == "BOOK_TWO_SIDED",
        "execution_liquidity_verified": False,
        "caveat": (
            "Quote evidence only; size, freshness, fees and settlement alignment are separate gates"
        ),
    }


def capture_named_baselines(
    *,
    ticker: str,
    market_original: bytes,
    market_sha256: str,
    market_received_at: datetime,
    model_input_as_of: datetime,
    orderbook_original: bytes | None = None,
    orderbook_sha256: str | None = None,
    orderbook_received_at: datetime | None = None,
    orderbook_url: str | None = None,
) -> dict[str, Any]:
    """Bind actual baseline output to exact originals before a later disk freeze.

    Listing-only None book is the named model's existing API, not an invented
    empty book. Its zero-size quote fallback remains labeled, never executable.
    """
    if not _aware(model_input_as_of) or not _aware(market_received_at):
        raise ValueError("BASELINE_AWARE_RECEIPTS_REQUIRED")
    if market_received_at > model_input_as_of:
        raise ValueError("BASELINE_FUTURE_RECEIPT")
    listing = _original(market_original, market_sha256)
    markets = listing.get("markets")
    if markets is None and "events" in listing:
        markets = event_markets(listing, ticker.split("-", 1)[0])
    if not isinstance(markets, list):
        raise ValueError("BASELINE_ORIGINAL_MARKET_LIST_REQUIRED")
    matches = [row for row in markets if isinstance(row, dict) and row.get("ticker") == ticker]
    if not ticker or len(matches) != 1:
        raise ValueError("BASELINE_EXACT_MARKET_IDENTITY_REQUIRED")
    market = matches[0]
    book = None
    if orderbook_original is None:
        if any(
            value is not None for value in (orderbook_sha256, orderbook_received_at, orderbook_url)
        ):
            raise ValueError("BASELINE_PARTIAL_BOOK_RECEIPT")
    else:
        if (
            orderbook_sha256 is None
            or orderbook_received_at is None
            or orderbook_url is None
            or not _aware(orderbook_received_at)
        ):
            raise ValueError("BASELINE_PARTIAL_BOOK_RECEIPT")
        if orderbook_received_at > model_input_as_of:
            raise ValueError("BASELINE_FUTURE_RECEIPT")
        url = urlparse(orderbook_url)
        if (
            url.scheme != "https"
            or url.netloc not in {"api.elections.kalshi.com", "external-api.kalshi.com"}
            or url.path != f"/trade-api/v2/markets/{ticker}/orderbook"
            or bool(url.fragment)
        ):
            raise ValueError("BASELINE_BOOK_REQUEST_IDENTITY_MISMATCH")
        book = _original(orderbook_original, orderbook_sha256)
    try:
        output = MarketImpliedForecaster().forecast(
            ForecastInput(ticker, model_input_as_of, market, book)
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("BASELINE_INVALID_NUMERICAL_INPUT") from exc
    if output is not None and (
        not output.yes_probability.is_finite() or not 0 <= output.yes_probability <= 1
    ):
        raise ValueError("BASELINE_INVALID_PROBABILITY")
    provenance = {
        "market_sha256": market_sha256,
        "market_received_at": market_received_at.isoformat(),
        "orderbook_sha256": orderbook_sha256,
        "orderbook_received_at": orderbook_received_at.isoformat()
        if orderbook_received_at
        else None,
        "orderbook_url": orderbook_url,
        "model_input_as_of": model_input_as_of.isoformat(),
    }
    named = {
        "model": "market_implied_v1",
        "model_invoked": True,
        "probability": float(output.yes_probability) if output else None,
        "status": "UNCALIBRATED_RESEARCH" if output else "UNAVAILABLE_NO_MARKET_PROBABILITY",
        "source": output.feature_json["source"] if output else None,
        "feature_json": output.feature_json if output else {},
        "input_provenance": provenance,
        "quote_quality": baseline_quote_quality(
            market, book, output.feature_json["source"] if output else None
        ),
        "execution_liquidity_verified": False,
        "caveat": "Named baseline can average zero-size listing quotes; not execution evidence",
    }
    return {
        "market_implied_v1": named,
        "crypto_v2": {
            "model": "crypto_v2",
            "model_invoked": False,
            "probability": None,
            "status": "UNAVAILABLE_MISSING_CAPTURED_INPUTS",
            "missing_inputs": [
                "point-in-time linked market confidence/components and selection records",
                "compatible CF feature rows with momentum/history and source selection provenance",
                "original snapshot context and crypto_v2 runtime thresholds/settings",
            ],
            "reason": "Standalone public capture does not contain original DB model inputs",
        },
    }
