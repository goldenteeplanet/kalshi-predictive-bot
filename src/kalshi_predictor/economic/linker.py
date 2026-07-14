import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import EconomicMarketLink, Market
from kalshi_predictor.economic.repository import (
    get_economic_events,
    insert_economic_market_link,
)

ECONOMIC_PATTERNS = {
    "cpi": (r"\bcpi\b", r"\binflation\b"),
    "fed": (r"\bfed\b", r"\bfomc\b", r"\bfederal reserve\b", r"\brates?\b", r"\binterest rate\b"),
    "jobs": (r"\bunemployment\b", r"\bjobs?\b", r"\bpayrolls?\b"),
    "gdp": (r"\bgdp\b", r"\brecession\b"),
}


@dataclass(frozen=True)
class EconomicLinkResult:
    markets_scanned: int
    links_created: int
    links_skipped_existing: int
    by_category: dict[str, int]


def link_economic_markets(
    session: Session,
    *,
    tickers: Iterable[str] | None = None,
    series_tickers: Iterable[str] | None = None,
    series_category_hints: Mapping[str, str] | None = None,
    limit: int | None = None,
) -> EconomicLinkResult:
    session.flush()
    events = get_economic_events(session)
    event_keys = {event.event_key: event.category for event in events}
    statement = select(Market).order_by(Market.ticker)
    ticker_set = {ticker for ticker in (tickers or []) if ticker}
    series_set = {series.upper() for series in (series_tickers or []) if series}
    if ticker_set:
        statement = statement.where(Market.ticker.in_(sorted(ticker_set)))
    if series_set:
        series_conditions = [Market.series_ticker.in_(sorted(series_set))]
        for series in sorted(series_set):
            series_conditions.append(Market.event_ticker.like(f"{series}-%"))
            series_conditions.append(Market.ticker.like(f"{series}-%"))
        statement = statement.where(or_(*series_conditions))
    if limit is not None:
        statement = statement.limit(limit)
    markets = list(session.scalars(statement))
    existing_link_tickers = set(
        session.scalars(
            select(EconomicMarketLink.ticker).where(
                EconomicMarketLink.ticker.in_([market.ticker for market in markets])
            )
        )
    )
    by_category: dict[str, int] = {}
    links = 0
    skipped_existing = 0
    for market in markets:
        if market.ticker in existing_link_tickers:
            skipped_existing += 1
            continue
        category, confidence, reason = detect_economic_market(
            market,
            series_category_hints=series_category_hints,
        )
        if category is None:
            continue
        event_key = _event_key_for_category(category, event_keys)
        insert_economic_market_link(
            session,
            ticker=market.ticker,
            event_key=event_key,
            category=category,
            confidence=confidence,
            reason=reason,
            raw_json={
                "ticker": market.ticker,
                "title": market.title,
                "series_ticker": market.series_ticker,
                "event_ticker": market.event_ticker,
                "series_category_hint": _series_hint_for_market(market, series_category_hints),
            },
        )
        links += 1
        by_category[category] = by_category.get(category, 0) + 1
        existing_link_tickers.add(market.ticker)
    return EconomicLinkResult(
        markets_scanned=len(markets),
        links_created=links,
        links_skipped_existing=skipped_existing,
        by_category=by_category,
    )


def detect_economic_market(
    market: Market,
    *,
    series_category_hints: Mapping[str, str] | None = None,
) -> tuple[str | None, Decimal, str]:
    text = _market_text(market)
    for category, patterns in ECONOMIC_PATTERNS.items():
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
            return category, Decimal("0.85"), f"{category.upper()} economic keyword match."
    hint = _series_hint_for_market(market, series_category_hints)
    if hint:
        return hint, Decimal("0.90"), f"{hint.upper()} Economics series metadata match."
    return None, Decimal("0"), "No economic keyword match."


def _event_key_for_category(category: str, event_keys: dict[str, str]) -> str:
    for event_key, event_category in event_keys.items():
        if event_key == category or event_category == category:
            return event_key
    return category


def _market_text(market: Market) -> str:
    raw = decode_json(market.raw_json)
    parts = [
        market.ticker,
        market.title,
        market.subtitle,
        market.series_ticker,
        market.event_ticker,
        market.rules_primary,
        market.rules_secondary,
        raw.get("rules_primary"),
        raw.get("rules_secondary"),
        raw.get("rules"),
    ]
    return " ".join(str(part or "") for part in parts)


def _series_hint_for_market(
    market: Market,
    series_category_hints: Mapping[str, str] | None,
) -> str | None:
    if not series_category_hints:
        return None
    normalized_hints = {
        str(series).upper(): str(category).strip().lower()
        for series, category in series_category_hints.items()
        if series and category
    }
    series_candidates = [
        market.series_ticker,
        _prefix(market.event_ticker),
        _prefix(market.ticker),
    ]
    for series in series_candidates:
        if not series:
            continue
        category = normalized_hints.get(str(series).upper())
        if category:
            return category
    return None


def _prefix(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).split("-", 1)[0]
