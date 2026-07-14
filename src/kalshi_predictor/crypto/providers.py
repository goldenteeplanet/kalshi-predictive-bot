from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from kalshi_predictor.crypto.assets import (
    coinbase_product_for_symbol,
    coingecko_id_for_symbol,
)
from kalshi_predictor.crypto.repository import normalize_symbol
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class CryptoQuote:
    symbol: str
    source: str
    observed_at: datetime
    price_usd: Decimal
    volume_24h: Decimal | None
    market_cap: Decimal | None
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class CryptoFetchResult:
    source: str
    quotes: list[CryptoQuote]
    errors: list[str]


def parse_coinbase_spot_response(
    symbol: str,
    payload: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> CryptoQuote:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Coinbase spot response missing data object.")
    amount = to_decimal(data.get("amount"))
    if amount is None:
        raise ValueError("Coinbase spot response missing numeric amount.")
    return CryptoQuote(
        symbol=normalize_symbol(str(data.get("base") or symbol)),
        source="coinbase",
        observed_at=observed_at or utc_now(),
        price_usd=amount,
        volume_24h=None,
        market_cap=None,
        raw_json=payload,
    )


def parse_coingecko_simple_price(
    symbol: str,
    payload: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> CryptoQuote:
    coin_id = _coingecko_id(symbol)
    data = payload.get(coin_id)
    if not isinstance(data, dict):
        raise ValueError(f"CoinGecko response missing {coin_id}.")
    price = to_decimal(data.get("usd"))
    if price is None:
        raise ValueError("CoinGecko response missing usd price.")
    return CryptoQuote(
        symbol=normalize_symbol(symbol),
        source="coingecko",
        observed_at=observed_at or utc_now(),
        price_usd=price,
        volume_24h=to_decimal(data.get("usd_24h_vol")),
        market_cap=to_decimal(data.get("usd_market_cap")),
        raw_json=payload,
    )


def fetch_crypto_quotes(
    symbols: list[str],
    *,
    source: str = "coinbase",
    timeout_seconds: float = 10.0,
) -> CryptoFetchResult:
    if source == "coinbase":
        return _fetch_coinbase(symbols, timeout_seconds=timeout_seconds)
    if source == "coingecko":
        return _fetch_coingecko(symbols, timeout_seconds=timeout_seconds)
    return CryptoFetchResult(
        source=source,
        quotes=[],
        errors=[f"Unsupported crypto source: {source}"],
    )


def _fetch_coinbase(symbols: list[str], *, timeout_seconds: float) -> CryptoFetchResult:
    quotes: list[CryptoQuote] = []
    errors: list[str] = []
    with httpx.Client(timeout=timeout_seconds) as client:
        for symbol in symbols:
            normalized = normalize_symbol(symbol)
            try:
                product = coinbase_product_for_symbol(normalized)
                response = client.get(
                    f"https://api.coinbase.com/v2/prices/{product}/spot"
                )
                response.raise_for_status()
                quotes.append(parse_coinbase_spot_response(normalized, response.json()))
            except Exception as exc:
                errors.append(f"{normalized}: {exc}")
    return CryptoFetchResult(source="coinbase", quotes=quotes, errors=errors)


def _fetch_coingecko(symbols: list[str], *, timeout_seconds: float) -> CryptoFetchResult:
    ids = ",".join(_coingecko_id(symbol) for symbol in symbols)
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids}&vs_currencies=usd&include_market_cap=true&include_24hr_vol=true"
    )
    try:
        response = httpx.get(url, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return CryptoFetchResult(source="coingecko", quotes=[], errors=[str(exc)])

    quotes: list[CryptoQuote] = []
    errors: list[str] = []
    for symbol in symbols:
        try:
            quotes.append(parse_coingecko_simple_price(symbol, payload))
        except Exception as exc:
            errors.append(f"{normalize_symbol(symbol)}: {exc}")
    return CryptoFetchResult(source="coingecko", quotes=quotes, errors=errors)


def _coingecko_id(symbol: str) -> str:
    return coingecko_id_for_symbol(normalize_symbol(symbol))
