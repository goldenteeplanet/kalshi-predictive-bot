from datetime import UTC
from decimal import Decimal
from typing import Any

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.utils.decimals import decimal_to_str, midpoint, to_decimal
from kalshi_predictor.utils.time import parse_datetime


def build_market_features(snapshot: MarketSnapshot) -> dict[str, Any]:
    raw_market = decode_json(snapshot.raw_market_json)
    best_yes_bid = to_decimal(snapshot.best_yes_bid)
    best_yes_ask = to_decimal(snapshot.best_yes_ask)
    midpoint_value = (
        midpoint(best_yes_bid, best_yes_ask)
        if best_yes_bid is not None and best_yes_ask is not None
        else None
    )
    close_time = parse_datetime(raw_market.get("close_time"))
    time_to_close_minutes = None
    if close_time is not None:
        captured_at = snapshot.captured_at
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        time_to_close_minutes = (close_time - captured_at).total_seconds() / 60

    return {
        "best_yes_bid": snapshot.best_yes_bid,
        "best_yes_ask": snapshot.best_yes_ask,
        "best_no_bid": snapshot.best_no_bid,
        "best_no_ask": snapshot.best_no_ask,
        "spread": snapshot.spread,
        "midpoint": decimal_to_str(midpoint_value),
        "volume": snapshot.volume_fp,
        "open_interest": snapshot.open_interest_fp,
        "liquidity": _string_or_none(raw_market.get("liquidity_dollars")),
        "time_to_close_minutes": _decimal_string(time_to_close_minutes),
        "market_status": snapshot.status,
        "market_category": _string_or_none(
            raw_market.get("category") or raw_market.get("market_type")
        ),
        "series_ticker": _string_or_none(raw_market.get("series_ticker")),
        "event_ticker": _string_or_none(raw_market.get("event_ticker")),
        "title": _string_or_none(raw_market.get("title")),
        "subtitle": _string_or_none(raw_market.get("subtitle")),
    }


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _decimal_string(value: float | int | Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_to_str(value)

