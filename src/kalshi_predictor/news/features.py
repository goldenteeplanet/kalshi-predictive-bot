from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import NewsItem, NewsMarketLink
from kalshi_predictor.news.repository import insert_news_feature, item_entities
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class NewsFeatureBuildSummary:
    tickers_processed: int
    links_scanned: int
    features_inserted: int


def build_news_features(
    session: Session,
    *,
    window_minutes: int | None = None,
    settings: Settings | None = None,
) -> NewsFeatureBuildSummary:
    resolved = settings or get_settings()
    resolved_window = window_minutes or resolved.news_default_window_minutes
    generated_at = utc_now()
    cutoff = generated_at - timedelta(minutes=resolved_window)
    rows = list(
        session.execute(
            select(NewsMarketLink, NewsItem)
            .join(NewsItem, NewsMarketLink.news_item_id == NewsItem.id)
            .where(
                NewsItem.ingested_at <= generated_at,
                or_(NewsItem.published_at.is_(None), NewsItem.published_at <= generated_at),
                or_(
                    NewsItem.published_at.is_(None),
                    NewsItem.published_at >= cutoff,
                    NewsItem.ingested_at >= cutoff,
                )
            )
            .order_by(NewsMarketLink.ticker, NewsItem.published_at, NewsItem.id)
        )
    )

    grouped: dict[str, list[tuple[NewsMarketLink, NewsItem]]] = defaultdict(list)
    for link, item in rows:
        grouped[link.ticker].append((link, item))

    inserted = 0
    for ticker, link_items in grouped.items():
        feature_payload = calculate_news_feature(
            link_items,
            window_minutes=resolved_window,
            min_importance=resolved.news_min_importance_score,
        )
        insert_news_feature(
            session,
            ticker=ticker,
            feature_window_minutes=resolved_window,
            news_count=feature_payload["news_count"],
            high_importance_count=feature_payload["high_importance_count"],
            avg_sentiment=feature_payload["avg_sentiment"],
            max_importance=feature_payload["max_importance"],
            freshness_score=feature_payload["freshness_score"],
            category_counts=feature_payload["category_counts"],
            entity_counts=feature_payload["entity_counts"],
            linked_news=feature_payload["linked_news"],
            raw_json=feature_payload,
        )
        inserted += 1

    return NewsFeatureBuildSummary(
        tickers_processed=len(grouped),
        links_scanned=len(rows),
        features_inserted=inserted,
    )


def calculate_news_feature(
    link_items: list[tuple[NewsMarketLink, NewsItem]],
    *,
    window_minutes: int,
    min_importance: Decimal,
) -> dict[str, Any]:
    sentiments: list[Decimal] = []
    importances: list[Decimal] = []
    freshness_values: list[Decimal] = []
    category_counts: Counter[str] = Counter()
    entity_counts: Counter[str] = Counter()
    linked_news: list[dict[str, Any]] = []

    for link, item in link_items:
        sentiment = to_decimal(item.sentiment_score) or Decimal("0")
        importance = to_decimal(item.importance_score) or Decimal("0")
        freshness = to_decimal(item.freshness_score) or Decimal("0")
        confidence = to_decimal(link.link_confidence) or Decimal("0")
        sentiments.append(sentiment)
        importances.append(importance)
        freshness_values.append(max(freshness * confidence, freshness))
        category_counts[item.category] += 1
        for entity in item_entities(item):
            entity_counts[entity] += 1
        linked_news.append(
            {
                "news_item_id": item.id,
                "title": item.title,
                "source": item.source,
                "category": item.category,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "sentiment_score": item.sentiment_score,
                "importance_score": item.importance_score,
                "freshness_score": item.freshness_score,
                "link_confidence": link.link_confidence,
                "link_reason": link.link_reason,
            }
        )

    news_count = len(link_items)
    avg_sentiment = (
        sum(sentiments, Decimal("0")) / Decimal(news_count) if news_count else Decimal("0")
    )
    max_importance = max(importances) if importances else Decimal("0")
    freshness_score = max(freshness_values) if freshness_values else Decimal("0")
    high_importance = sum(1 for value in importances if value >= min_importance)
    return {
        "window_minutes": window_minutes,
        "news_count": news_count,
        "high_importance_count": high_importance,
        "avg_sentiment": avg_sentiment.quantize(Decimal("0.0001")),
        "max_importance": max_importance.quantize(Decimal("0.0001")),
        "freshness_score": freshness_score.quantize(Decimal("0.0001")),
        "category_counts": dict(category_counts),
        "entity_counts": dict(entity_counts),
        "linked_news": linked_news,
    }
