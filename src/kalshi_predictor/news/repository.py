from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import NewsFeature, NewsItem, NewsMarketLink, NewsSignal
from kalshi_predictor.news.classifier import classify_news_item
from kalshi_predictor.source_safety import canonicalize_source_url, validate_point_in_time
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now


def upsert_news_item(
    session: Session,
    payload: Mapping[str, Any],
    *,
    ingested_at: datetime | None = None,
) -> tuple[NewsItem, bool]:
    raw_payload = dict(payload)
    title = _required_text(raw_payload.get("title"), "title")
    source = str(raw_payload.get("source") or "manual").strip() or "manual"
    source_url = canonicalize_source_url(
        _str_or_none(
            raw_payload.get("canonical_url")
            or raw_payload.get("source_url")
            or raw_payload.get("url")
        )
    )
    published_at = parse_datetime(raw_payload.get("published_at"))
    resolved_ingested_at = (
        ingested_at or parse_datetime(raw_payload.get("ingested_at")) or utc_now()
    )
    available_at = parse_datetime(raw_payload.get("available_at")) or resolved_ingested_at
    if published_at is not None:
        validate_point_in_time(
            published_at=published_at,
            available_at=available_at,
            ingested_at=resolved_ingested_at,
        )
    existing = _pending_news_item(session, source_url, title, published_at) or _existing_news_item(
        session,
        source=source,
        source_url=source_url,
        title=title,
        published_at=published_at,
    )
    if existing is not None:
        return existing, False

    classification = classify_news_item({**raw_payload, "published_at": published_at})
    item = NewsItem(
        source=source,
        source_url=source_url,
        published_at=published_at,
        ingested_at=resolved_ingested_at,
        title=title,
        summary=_str_or_none(raw_payload.get("summary")),
        body=_str_or_none(raw_payload.get("body")),
        author=_str_or_none(raw_payload.get("author")),
        category=classification["category"],
        entities_json=encode_json({"entities": classification["entities"]}),
        sentiment_score=decimal_to_str(classification["sentiment_score"]) or "0",
        importance_score=decimal_to_str(classification["importance_score"]) or "0",
        freshness_score=decimal_to_str(classification["freshness_score"]) or "0",
        raw_json=encode_json(
            {
                **raw_payload,
                "canonical_url": source_url,
                "available_at": available_at.isoformat(),
                "classification": classification,
            }
        ),
    )
    session.add(item)
    session.flush()
    return item, True


def insert_news_market_link(
    session: Session,
    *,
    news_item_id: int,
    ticker: str,
    link_confidence: Any,
    link_reason: str,
    matched_terms: list[str],
    raw_json: Mapping[str, Any] | None = None,
    created_at: datetime | None = None,
) -> tuple[NewsMarketLink, bool]:
    existing = _pending_news_market_link(session, news_item_id, ticker) or session.scalar(
        select(NewsMarketLink).where(
            NewsMarketLink.news_item_id == news_item_id,
            NewsMarketLink.ticker == ticker,
        )
    )
    if existing is not None:
        return existing, False
    link = NewsMarketLink(
        created_at=created_at or utc_now(),
        news_item_id=news_item_id,
        ticker=ticker,
        link_confidence=decimal_to_str(link_confidence) or "0",
        link_reason=link_reason,
        matched_terms_json=encode_json({"matched_terms": matched_terms}),
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(link)
    session.flush()
    return link, True


def insert_news_feature(
    session: Session,
    *,
    ticker: str,
    feature_window_minutes: int,
    news_count: int,
    high_importance_count: int,
    avg_sentiment: Any,
    max_importance: Any,
    freshness_score: Any,
    category_counts: Mapping[str, int],
    entity_counts: Mapping[str, int],
    linked_news: list[dict[str, Any]],
    raw_json: Mapping[str, Any] | None = None,
    created_at: datetime | None = None,
) -> NewsFeature:
    feature = NewsFeature(
        created_at=created_at or utc_now(),
        ticker=ticker,
        feature_window_minutes=feature_window_minutes,
        news_count=news_count,
        high_importance_count=high_importance_count,
        avg_sentiment=decimal_to_str(avg_sentiment),
        max_importance=decimal_to_str(max_importance),
        freshness_score=decimal_to_str(freshness_score),
        category_counts_json=encode_json(dict(category_counts)),
        entity_counts_json=encode_json(dict(entity_counts)),
        linked_news_json=encode_json({"items": linked_news}),
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(feature)
    session.flush()
    return feature


def insert_news_signal(
    session: Session,
    *,
    ticker: str,
    signal_name: str,
    signal_strength: Any,
    signal_direction: str | None,
    confidence: Any,
    explanation: str,
    raw_json: Mapping[str, Any] | None = None,
    created_at: datetime | None = None,
) -> NewsSignal:
    signal = NewsSignal(
        created_at=created_at or utc_now(),
        ticker=ticker,
        signal_name=signal_name,
        signal_strength=decimal_to_str(signal_strength) or "0",
        signal_direction=signal_direction,
        confidence=decimal_to_str(confidence) or "0",
        explanation=explanation,
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(signal)
    session.flush()
    return signal


def recent_news_items(session: Session, *, limit: int = 50) -> list[NewsItem]:
    return list(
        session.scalars(
            select(NewsItem)
            .order_by(desc(NewsItem.published_at), desc(NewsItem.ingested_at), desc(NewsItem.id))
            .limit(limit)
        )
    )


def all_news_items(session: Session) -> list[NewsItem]:
    return list(session.scalars(select(NewsItem).order_by(NewsItem.id)))


def news_links_for_item(session: Session, news_item_id: int) -> list[NewsMarketLink]:
    return list(
        session.scalars(
            select(NewsMarketLink)
            .where(NewsMarketLink.news_item_id == news_item_id)
            .order_by(desc(NewsMarketLink.link_confidence), NewsMarketLink.ticker)
        )
    )


def news_links_for_ticker(
    session: Session,
    ticker: str,
    *,
    limit: int = 20,
) -> list[NewsMarketLink]:
    return list(
        session.scalars(
            select(NewsMarketLink)
            .where(NewsMarketLink.ticker == ticker)
            .order_by(desc(NewsMarketLink.created_at), desc(NewsMarketLink.id))
            .limit(limit)
        )
    )


def latest_news_feature(session: Session, ticker: str) -> NewsFeature | None:
    return session.scalar(
        select(NewsFeature)
        .where(NewsFeature.ticker == ticker)
        .order_by(desc(NewsFeature.created_at), desc(NewsFeature.id))
        .limit(1)
    )


def latest_news_features(session: Session, *, limit: int | None = None) -> list[NewsFeature]:
    rows = list(
        session.scalars(
            select(NewsFeature).order_by(
                desc(NewsFeature.created_at),
                desc(NewsFeature.id),
            )
        )
    )
    seen: set[str] = set()
    latest: list[NewsFeature] = []
    for row in rows:
        if row.ticker in seen:
            continue
        seen.add(row.ticker)
        latest.append(row)
        if limit is not None and len(latest) >= limit:
            break
    return latest


def latest_news_signals_for_ticker(
    session: Session,
    ticker: str,
    *,
    limit: int = 3,
) -> list[NewsSignal]:
    return list(
        session.scalars(
            select(NewsSignal)
            .where(NewsSignal.ticker == ticker)
            .order_by(desc(NewsSignal.created_at), desc(NewsSignal.id))
            .limit(limit)
        )
    )


def recent_news_signals(session: Session, *, limit: int = 20) -> list[NewsSignal]:
    return list(
        session.scalars(
            select(NewsSignal)
            .order_by(desc(NewsSignal.created_at), desc(NewsSignal.id))
            .limit(limit)
        )
    )


def news_dashboard_summary(session: Session, *, limit: int = 20) -> dict[str, Any]:
    items = recent_news_items(session, limit=limit)
    signals = recent_news_signals(session, limit=limit)
    links = list(
        session.scalars(
            select(NewsMarketLink)
            .order_by(desc(NewsMarketLink.created_at), desc(NewsMarketLink.id))
            .limit(limit)
        )
    )
    category_counts = Counter(item.category for item in all_news_items(session))
    total_items = int(session.scalar(select(func.count(NewsItem.id))) or 0)
    total_links = int(session.scalar(select(func.count(NewsMarketLink.id))) or 0)
    total_features = int(session.scalar(select(func.count(NewsFeature.id))) or 0)
    total_signals = int(session.scalar(select(func.count(NewsSignal.id))) or 0)
    return {
        "summary": {
            "items": total_items,
            "links": total_links,
            "features": total_features,
            "signals": total_signals,
            "categories": dict(category_counts),
        },
        "latest_items": [_item_row(item) for item in items],
        "latest_links": [_link_row(link) for link in links],
        "latest_signals": [_signal_row(signal) for signal in signals],
    }


def item_entities(item: NewsItem) -> list[str]:
    decoded = decode_json(item.entities_json)
    entities = decoded.get("entities")
    return [str(entity) for entity in entities] if isinstance(entities, list) else []


def feature_linked_news(feature: NewsFeature) -> list[dict[str, Any]]:
    decoded = decode_json(feature.linked_news_json)
    items = decoded.get("items")
    return items if isinstance(items, list) else []


def _item_row(item: NewsItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": item.source,
        "source_url": item.source_url,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "ingested_at": item.ingested_at.isoformat(),
        "title": item.title,
        "summary": item.summary,
        "category": item.category,
        "entities": item_entities(item),
        "sentiment_score": item.sentiment_score,
        "importance_score": item.importance_score,
        "freshness_score": item.freshness_score,
        "links": [],
    }


def _link_row(link: NewsMarketLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "news_item_id": link.news_item_id,
        "ticker": link.ticker,
        "confidence": link.link_confidence,
        "reason": link.link_reason,
        "matched_terms": decode_json(link.matched_terms_json).get("matched_terms", []),
        "created_at": link.created_at.isoformat(),
    }


def _signal_row(signal: NewsSignal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "ticker": signal.ticker,
        "signal_name": signal.signal_name,
        "signal_strength": signal.signal_strength,
        "signal_direction": signal.signal_direction,
        "confidence": signal.confidence,
        "explanation": signal.explanation,
        "created_at": signal.created_at.isoformat(),
    }


def _existing_news_item(
    session: Session,
    *,
    source: str,
    source_url: str | None,
    title: str,
    published_at: datetime | None,
) -> NewsItem | None:
    if source_url:
        existing = session.scalar(select(NewsItem).where(NewsItem.source_url == source_url))
        if existing is not None:
            return existing
    statement = select(NewsItem).where(NewsItem.source == source, NewsItem.title == title)
    if published_at is not None:
        statement = statement.where(NewsItem.published_at == published_at)
    return session.scalar(statement.order_by(desc(NewsItem.id)).limit(1))


def _pending_news_item(
    session: Session,
    source_url: str | None,
    title: str,
    published_at: datetime | None,
) -> NewsItem | None:
    for item in session.new:
        if not isinstance(item, NewsItem):
            continue
        if source_url and item.source_url == source_url:
            return item
        if item.title == title and item.published_at == published_at:
            return item
    return None


def _pending_news_market_link(
    session: Session,
    news_item_id: int,
    ticker: str,
) -> NewsMarketLink | None:
    for item in session.new:
        if (
            isinstance(item, NewsMarketLink)
            and item.news_item_id == news_item_id
            and item.ticker == ticker
        ):
            return item
    return None


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Missing required news field: {field}")
    return text


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def decimal_or_zero(value: Any) -> Decimal:
    return to_decimal(value) or Decimal("0")
