import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, NewsItem
from kalshi_predictor.news.repository import all_news_items, insert_news_market_link, item_entities
from kalshi_predictor.utils.decimals import to_decimal

CATEGORY_MARKET_TERMS = {
    "crypto": ("btc", "bitcoin", "eth", "ethereum", "crypto", "coinbase"),
    "weather": (
        "weather",
        "temperature",
        "rain",
        "snow",
        "storm",
        "hurricane",
        "noaa",
        "nhc",
    ),
    "economic": (
        "fed",
        "fomc",
        "cpi",
        "inflation",
        "rates",
        "interest rate",
        "jobs",
        "unemployment",
        "payroll",
        "gdp",
    ),
    "sports": ("mlb", "nba", "nfl", "nhl", "injury", "game", "team"),
    "company": ("stock", "shares", "earnings", "company", "sec"),
    "politics": ("election", "president", "senate", "congress", "house"),
    "geopolitical": ("oil", "gas", "tariff", "war", "sanction", "energy"),
}

ENTITY_ALIASES = {
    "BTC": ("btc", "bitcoin"),
    "ETH": ("eth", "ethereum"),
    "Fed": ("fed", "fomc", "federal reserve", "powell"),
    "Interest Rates": ("interest rate", "rates", "rate decision"),
    "CPI": ("cpi", "inflation"),
    "Jobs": ("jobs", "unemployment", "payrolls", "payroll"),
    "Hurricane": ("hurricane", "nhc"),
    "Storm": ("storm", "rain", "snow", "temperature"),
    "Oil": ("oil", "gas", "energy"),
    "MLB": ("mlb", "baseball"),
    "NBA": ("nba", "basketball"),
    "NFL": ("nfl", "football"),
    "NHL": ("nhl", "hockey"),
}


@dataclass(frozen=True)
class NewsLinkSummary:
    news_items_scanned: int
    markets_scanned: int
    links_created: int
    links_by_category: dict[str, int] = field(default_factory=dict)


def link_news_markets(
    session: Session,
    *,
    settings: Settings | None = None,
) -> NewsLinkSummary:
    resolved = settings or get_settings()
    min_confidence = resolved.news_min_link_confidence
    items = all_news_items(session)
    markets = list(session.scalars(select(Market).order_by(Market.ticker)))
    links_created = 0
    by_category: Counter[str] = Counter()

    for item in items:
        for market in markets:
            confidence, reason, matched_terms = score_news_market_link(item, market)
            if confidence < min_confidence:
                continue
            _, created = insert_news_market_link(
                session,
                news_item_id=int(item.id),
                ticker=market.ticker,
                link_confidence=confidence,
                link_reason=reason,
                matched_terms=matched_terms,
                raw_json={
                    "news_item_id": item.id,
                    "ticker": market.ticker,
                    "category": item.category,
                    "market_title": market.title,
                },
            )
            if created:
                links_created += 1
                by_category[item.category] += 1

    return NewsLinkSummary(
        news_items_scanned=len(items),
        markets_scanned=len(markets),
        links_created=links_created,
        links_by_category=dict(by_category),
    )


def score_news_market_link(item: NewsItem, market: Market) -> tuple[Decimal, str, list[str]]:
    market_text = _market_text(market).lower()
    news_text = _news_text(item).lower()
    score = Decimal("0")
    matched: set[str] = set()

    category_terms = CATEGORY_MARKET_TERMS.get(item.category, ())
    category_matches = [term for term in category_terms if term in market_text]
    if category_matches:
        score += Decimal("0.35")
        matched.update(category_matches[:4])

    for entity in item_entities(item):
        aliases = ENTITY_ALIASES.get(entity, (entity.lower(),))
        entity_matches = [alias for alias in aliases if alias in market_text]
        if entity_matches:
            score += Decimal("0.30")
            matched.add(entity)
            matched.update(entity_matches[:2])

    overlap = _keyword_overlap(news_text, market_text)
    if overlap:
        score += min(Decimal(len(overlap)) * Decimal("0.04"), Decimal("0.25"))
        matched.update(sorted(overlap)[:6])

    ticker_bonus = _ticker_entity_bonus(item, market)
    if ticker_bonus:
        score += ticker_bonus
        matched.add(market.ticker)

    confidence = min(score, Decimal("1.00")).quantize(Decimal("0.0001"))
    if not matched:
        return Decimal("0"), "No market-relevant news terms matched.", []
    reason = (
        f"{item.category.title()} news matched {len(matched)} term(s) in market text."
    )
    return confidence, reason, sorted(matched)


def _market_text(market: Market) -> str:
    raw = decode_json(market.raw_json)
    parts = (
        market.ticker,
        market.title,
        market.subtitle,
        market.series_ticker,
        market.event_ticker,
        market.rules_primary,
        market.rules_secondary,
        raw.get("rules"),
        raw.get("rules_primary"),
        raw.get("rules_secondary"),
    )
    return " ".join(str(part or "") for part in parts)


def _news_text(item: NewsItem) -> str:
    return " ".join(str(part or "") for part in (item.title, item.summary, item.body))


def _keyword_overlap(news_text: str, market_text: str) -> set[str]:
    news_terms = {
        token
        for token in re.findall(r"[a-z0-9]+", news_text)
        if len(token) >= 4 and token not in {"will", "this", "that", "with", "from"}
    }
    market_terms = {
        token
        for token in re.findall(r"[a-z0-9]+", market_text)
        if len(token) >= 4 and token not in {"will", "this", "that", "with", "from"}
    }
    return news_terms & market_terms


def _ticker_entity_bonus(item: NewsItem, market: Market) -> Decimal:
    text = f"{market.ticker} {market.series_ticker or ''} {market.event_ticker or ''}".upper()
    for entity in item_entities(item):
        if len(entity) >= 2 and entity.upper().replace(" ", "") in text:
            return Decimal("0.20")
    importance = to_decimal(item.importance_score) or Decimal("0")
    if item.category == "economic" and importance >= Decimal("0.70") and "FED" in text:
        return Decimal("0.20")
    return Decimal("0")
