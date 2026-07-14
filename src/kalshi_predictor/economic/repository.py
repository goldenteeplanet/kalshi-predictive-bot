from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import case, desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import EconomicEvent, EconomicFeature, EconomicMarketLink
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def normalize_event_key(event_key: str) -> str:
    normalized = event_key.strip().lower().replace(" ", "_").replace("-", "_")
    return "_".join(part for part in normalized.split("_") if part)


def insert_economic_event(
    session: Session,
    *,
    event_key: str,
    source: str,
    event_time: datetime,
    category: str,
    title: str,
    actual_value: Any = None,
    forecast_value: Any = None,
    previous_value: Any = None,
    raw_json: Mapping[str, Any] | None = None,
) -> EconomicEvent:
    event = EconomicEvent(
        event_key=normalize_event_key(event_key),
        source=source,
        event_time=event_time,
        category=category.lower(),
        title=title,
        actual_value=decimal_to_str(actual_value),
        forecast_value=decimal_to_str(forecast_value),
        previous_value=decimal_to_str(previous_value),
        raw_json=encode_json(dict(raw_json or {})),
        created_at=utc_now(),
    )
    session.add(event)
    session.flush()
    return event


def get_economic_events(session: Session, *, limit: int | None = None) -> list[EconomicEvent]:
    statement = select(EconomicEvent).order_by(EconomicEvent.event_time, EconomicEvent.id)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def get_latest_economic_event(session: Session, event_key: str) -> EconomicEvent | None:
    return session.scalar(
        select(EconomicEvent)
        .where(EconomicEvent.event_key == normalize_event_key(event_key))
        .order_by(desc(EconomicEvent.event_time), desc(EconomicEvent.id))
        .limit(1)
    )


def insert_economic_feature(
    session: Session,
    *,
    event_key: str,
    generated_at: datetime,
    category: str,
    surprise_score: Any = None,
    direction: str = "NEUTRAL",
    confidence_score: Any = None,
    raw_json: Mapping[str, Any] | None = None,
) -> EconomicFeature:
    feature = EconomicFeature(
        event_key=normalize_event_key(event_key),
        generated_at=generated_at,
        category=category.lower(),
        surprise_score=decimal_to_str(surprise_score),
        direction=direction.upper(),
        confidence_score=decimal_to_str(confidence_score),
        raw_json=encode_json(dict(raw_json or {})),
        created_at=utc_now(),
    )
    session.add(feature)
    session.flush()
    return feature


def get_latest_economic_feature(session: Session, event_key: str) -> EconomicFeature | None:
    return session.scalar(
        select(EconomicFeature)
        .where(EconomicFeature.event_key == normalize_event_key(event_key))
        .order_by(
            case((EconomicFeature.surprise_score.is_(None), 1), else_=0),
            desc(EconomicFeature.generated_at),
            desc(EconomicFeature.id),
        )
        .limit(1)
    )


def insert_economic_market_link(
    session: Session,
    *,
    ticker: str,
    event_key: str,
    category: str,
    confidence: Any,
    reason: str,
    raw_json: Mapping[str, Any] | None = None,
) -> EconomicMarketLink:
    link = EconomicMarketLink(
        ticker=ticker,
        event_key=normalize_event_key(event_key),
        detected_at=utc_now(),
        category=category.lower(),
        confidence=decimal_to_str(confidence) or "0",
        reason=reason,
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(link)
    session.flush()
    return link


def get_latest_economic_link_for_ticker(
    session: Session,
    ticker: str,
) -> EconomicMarketLink | None:
    return session.scalar(
        select(EconomicMarketLink)
        .where(EconomicMarketLink.ticker == ticker)
        .order_by(desc(EconomicMarketLink.detected_at), desc(EconomicMarketLink.id))
        .limit(1)
    )
