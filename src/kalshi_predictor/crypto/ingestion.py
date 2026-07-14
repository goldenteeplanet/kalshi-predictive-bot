from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.crypto.providers import CryptoFetchResult, CryptoQuote, fetch_crypto_quotes
from kalshi_predictor.crypto.repository import insert_crypto_price, normalize_symbol
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now


@dataclass(frozen=True)
class CryptoIngestionSummary:
    source: str
    prices_inserted: int
    errors: list[str]


def ingest_crypto_quotes(
    session: Session,
    *,
    symbols: list[str],
    source: str = "coinbase",
) -> CryptoIngestionSummary:
    result = fetch_crypto_quotes(symbols, source=source)
    return store_crypto_fetch_result(session, result)


def store_crypto_fetch_result(
    session: Session,
    result: CryptoFetchResult,
) -> CryptoIngestionSummary:
    count = 0
    for quote in result.quotes:
        _insert_quote(session, quote)
        count += 1
    return CryptoIngestionSummary(
        source=result.source,
        prices_inserted=count,
        errors=result.errors,
    )


def ingest_manual_crypto_json(
    session: Session,
    payload: Mapping[str, Any],
    *,
    source: str = "manual",
) -> CryptoIngestionSummary:
    records = _extract_manual_records(payload)
    errors: list[str] = []
    count = 0
    for record in records:
        symbol = record.get("symbol") or record.get("base") or payload.get("symbol")
        price = (
            record.get("price_usd")
            or record.get("price")
            or record.get("amount")
            or record.get("usd")
        )
        price_value = to_decimal(price)
        if symbol is None or price_value is None:
            errors.append(f"Skipped record with missing symbol or price: {record}")
            continue
        observed_at = parse_datetime(
            record.get("observed_at") or record.get("timestamp") or payload.get("observed_at")
        ) or utc_now()
        insert_crypto_price(
            session,
            symbol=normalize_symbol(str(symbol)),
            source=str(record.get("source") or payload.get("source") or source),
            observed_at=observed_at,
            price_usd=price_value,
            volume_24h=record.get("volume_24h") or record.get("usd_24h_vol"),
            market_cap=record.get("market_cap") or record.get("usd_market_cap"),
            raw_json=dict(record),
        )
        count += 1
    return CryptoIngestionSummary(source=source, prices_inserted=count, errors=errors)


def _insert_quote(session: Session, quote: CryptoQuote) -> None:
    insert_crypto_price(
        session,
        symbol=quote.symbol,
        source=quote.source,
        observed_at=quote.observed_at,
        price_usd=quote.price_usd,
        volume_24h=quote.volume_24h,
        market_cap=quote.market_cap,
        raw_json=quote.raw_json,
    )


def _extract_manual_records(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    prices = payload.get("prices")
    if isinstance(prices, list):
        return [item for item in prices if isinstance(item, Mapping)]
    data = payload.get("data")
    if isinstance(data, Mapping) and "amount" in data:
        return [data]
    return [payload]

