from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import CryptoFeature, CryptoMarketLink, CryptoPrice
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def parse_symbols(symbols: str) -> list[str]:
    return [normalize_symbol(symbol) for symbol in symbols.split(",") if symbol.strip()]


def insert_crypto_price(
    session: Session,
    *,
    symbol: str,
    source: str,
    observed_at: datetime,
    price_usd: Any,
    volume_24h: Any = None,
    market_cap: Any = None,
    raw_json: Mapping[str, Any] | None = None,
) -> CryptoPrice:
    price = CryptoPrice(
        symbol=normalize_symbol(symbol),
        source=source,
        observed_at=observed_at,
        price_usd=decimal_to_str(price_usd) or "0",
        volume_24h=decimal_to_str(volume_24h),
        market_cap=decimal_to_str(market_cap),
        raw_json=encode_json(dict(raw_json or {})),
        created_at=utc_now(),
    )
    session.add(price)
    session.flush()
    return price


def get_crypto_prices(
    session: Session,
    symbol: str,
    *,
    limit: int | None = None,
) -> list[CryptoPrice]:
    statement = (
        select(CryptoPrice)
        .where(CryptoPrice.symbol == normalize_symbol(symbol))
        .order_by(CryptoPrice.observed_at)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def get_latest_crypto_price(session: Session, symbol: str) -> CryptoPrice | None:
    return session.scalar(
        select(CryptoPrice)
        .where(CryptoPrice.symbol == normalize_symbol(symbol))
        .order_by(desc(CryptoPrice.observed_at), desc(CryptoPrice.id))
        .limit(1)
    )


def insert_crypto_features(
    session: Session,
    *,
    symbol: str,
    source: str,
    generated_at: datetime,
    window_minutes: int,
    features: Mapping[str, Any],
    raw_json: Mapping[str, Any] | None = None,
) -> CryptoFeature:
    feature = CryptoFeature(
        symbol=normalize_symbol(symbol),
        source=source,
        generated_at=generated_at,
        window_minutes=window_minutes,
        price=decimal_to_str(features.get("price")),
        return_5m=decimal_to_str(features.get("return_5m")),
        return_15m=decimal_to_str(features.get("return_15m")),
        return_1h=decimal_to_str(features.get("return_1h")),
        return_4h=decimal_to_str(features.get("return_4h")),
        return_24h=decimal_to_str(features.get("return_24h")),
        volatility_1h=decimal_to_str(features.get("volatility_1h")),
        volatility_4h=decimal_to_str(features.get("volatility_4h")),
        volatility_24h=decimal_to_str(features.get("volatility_24h")),
        momentum_score=decimal_to_str(features.get("momentum_score")),
        trend_direction=str(features.get("trend_direction") or "UNKNOWN"),
        raw_json=encode_json(dict(raw_json or features)),
        created_at=utc_now(),
    )
    session.add(feature)
    session.flush()
    return feature


def get_latest_crypto_features(session: Session, symbol: str) -> CryptoFeature | None:
    return session.scalar(
        select(CryptoFeature)
        .where(CryptoFeature.symbol == normalize_symbol(symbol))
        .order_by(desc(CryptoFeature.generated_at), desc(CryptoFeature.id))
        .limit(1)
    )


def insert_crypto_market_link(
    session: Session,
    *,
    ticker: str,
    symbol: str,
    confidence: Any,
    reason: str,
    raw_json: Mapping[str, Any] | None = None,
    detected_at: datetime | None = None,
) -> CryptoMarketLink:
    link = CryptoMarketLink(
        ticker=ticker,
        symbol=normalize_symbol(symbol),
        detected_at=detected_at or utc_now(),
        confidence=decimal_to_str(confidence) or "0",
        reason=reason,
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(link)
    session.flush()
    return link


def get_latest_crypto_link_for_ticker(session: Session, ticker: str) -> CryptoMarketLink | None:
    return session.scalar(
        select(CryptoMarketLink)
        .where(CryptoMarketLink.ticker == ticker)
        .order_by(desc(CryptoMarketLink.detected_at), desc(CryptoMarketLink.id))
        .limit(1)
    )


def get_crypto_links(session: Session, *, limit: int | None = None) -> list[CryptoMarketLink]:
    statement = select(CryptoMarketLink).order_by(
        desc(CryptoMarketLink.detected_at),
        CryptoMarketLink.ticker,
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))

