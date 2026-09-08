"""Read-only qualification using the established REST protocol and safety gates."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_predictor.kalshi.orderbook import parse_orderbook, usable_bid_ask_book
from kalshi_predictor.phase3ap import MIN_EXECUTABLE_LIQUIDITY_SCORE, QUOTE_STALE_AFTER_MINUTES


def qualify_book(
    payload: dict[str, Any],
    *,
    received_at: datetime,
    now: datetime,
    max_spread: Decimal,
    liquidity_score: Decimal | None = None,
    price_ranges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """No summary-price fallback; both executable sides retain canonical gates."""
    age = (now - received_at).total_seconds()
    invalid = _invalid_levels(payload, price_ranges)
    stale = age < 0 or age > float(QUOTE_STALE_AFTER_MINUTES * 60)
    prices = parse_orderbook(payload) if not invalid else None
    sides = {}
    for side in ("YES", "NO"):
        book = usable_bid_ask_book(
            payload if not invalid else {},
            side=side,
            liquidity_score=liquidity_score,
            min_liquidity_score=MIN_EXECUTABLE_LIQUIDITY_SCORE,
            max_spread=max_spread,
        )
        blocker = (
            "INVALID_BOOK_OR_TICK"
            if invalid
            else "STALE_ORDERBOOK"
            if stale
            else "TICK_RULE_UNVERIFIED"
            if not price_ranges
            else None
            if book.usable
            else book.state
        )
        sides[side] = {
            "executable": blocker is None,
            "first_blocker": blocker,
            "reason": invalid or book.reason,
            "bid": _str(book.bid_price),
            "ask": _str(book.ask_price),
            "bid_depth": _str(book.bid_depth),
            "ask_depth": _str(book.ask_depth),
            "spread": _str(book.spread),
            "buy_price_source": "NO_BID_COMPLEMENT" if side == "YES" else "YES_BID_COMPLEMENT",
        }
    return {
        "received_at": received_at.isoformat(),
        "receipt_age_seconds": age,
        "exchange_book_update_time": None,
        "freshness_basis": "PUBLIC_REST_RECEIPT",
        "yes_bid": _str(prices.best_yes_bid) if prices else None,
        "yes_ask": _str(prices.best_yes_ask) if prices else None,
        "no_bid": _str(prices.best_no_bid) if prices else None,
        "no_ask": _str(prices.best_no_ask) if prices else None,
        "liquidity_score": _str(liquidity_score),
        "sides": sides,
        "executable": any(value["executable"] for value in sides.values()),
        "tick_status": "VERIFIED" if price_ranges and not invalid else "UNVERIFIED",
        "thresholds": {
            "max_spread": str(max_spread),
            "min_depth": "1",
            "minimum_liquidity_score": str(MIN_EXECUTABLE_LIQUIDITY_SCORE),
            "max_age_minutes": str(QUOTE_STALE_AFTER_MINUTES),
        },
    }


def _str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _invalid_levels(payload: dict[str, Any], ranges: list[dict[str, Any]] | None) -> str | None:
    container = payload.get("orderbook_fp", payload.get("orderbook", payload))
    if not isinstance(container, dict):
        return "Invalid container"
    dollars = "yes_dollars" in container or "no_dollars" in container
    try:
        for side in ("yes", "no"):
            levels = container.get(f"{side}_dollars" if dollars else side) or []
            if not isinstance(levels, list):
                return "Invalid levels"
            seen = set()
            for level in levels:
                price, quantity = Decimal(str(level[0])), Decimal(str(level[1]))
                if not dollars:
                    price /= 100
                if not price.is_finite() or not quantity.is_finite():
                    return "Nonfinite price or quantity"
                if not 0 < price < 1 or quantity <= 0 or price in seen:
                    return "Invalid price, quantity, or duplicate level"
                seen.add(price)
                if ranges and not any(_on_tick(price, item) for item in ranges):
                    return "Price outside authoritative tick ranges"
    except (InvalidOperation, TypeError, IndexError, KeyError, ValueError):
        return "Malformed price or tick definition"
    return None


def _on_tick(price: Decimal, item: dict[str, Any]) -> bool:
    start, end, step = (Decimal(str(item[key])) for key in ("start", "end", "step"))
    return bool(
        start.is_finite()
        and end.is_finite()
        and step.is_finite()
        and step > 0
        and start <= price <= end
        and (price - start) % step == 0
    )
