from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from statistics import pstdev
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.crypto.repository import (
    get_crypto_prices,
    insert_crypto_features,
    normalize_symbol,
)
from kalshi_predictor.data.schema import CryptoPrice
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

RETURN_WINDOWS = {
    "return_5m": 5,
    "return_15m": 15,
    "return_1h": 60,
    "return_4h": 240,
    "return_24h": 1440,
}

VOL_WINDOWS = {
    "volatility_1h": 60,
    "volatility_4h": 240,
    "volatility_24h": 1440,
}


@dataclass(frozen=True)
class CryptoFeatureBuildSummary:
    symbols_processed: int
    features_inserted: int


def build_crypto_features(
    session: Session,
    *,
    symbols: Iterable[str],
    source: str = "stored_prices",
    window_minutes: int = 1440,
) -> CryptoFeatureBuildSummary:
    symbol_list = [normalize_symbol(symbol) for symbol in symbols]
    inserted = 0
    for symbol in symbol_list:
        prices = get_crypto_prices(session, symbol)
        features = calculate_crypto_features(prices, window_minutes=window_minutes)
        raw_json = {
            **features,
            "asset": symbol,
            "symbol": symbol,
            "price_source": source,
            "feature_version": "crypto_features_v2_point_in_time",
            "quality_flags": _quality_flags(features),
            "source_first_observed_at": (
                prices[0].observed_at.isoformat() if prices else None
            ),
            "source_latest_observed_at": (
                prices[-1].observed_at.isoformat() if prices else None
            ),
            "source_observation_ref": (
                {
                    "table": "crypto_prices",
                    "id": prices[-1].id,
                    "symbol": prices[-1].symbol,
                    "source": prices[-1].source,
                    "observed_at": prices[-1].observed_at.isoformat(),
                }
                if prices else None
            ),
        }
        insert_crypto_features(
            session,
            symbol=symbol,
            source=source,
            generated_at=utc_now(),
            window_minutes=window_minutes,
            features=features,
            raw_json=raw_json,
        )
        inserted += 1
    return CryptoFeatureBuildSummary(
        symbols_processed=len(symbol_list),
        features_inserted=inserted,
    )


def calculate_crypto_features(
    prices: list[CryptoPrice],
    *,
    window_minutes: int = 1440,
) -> dict[str, Any]:
    ordered = sorted(prices, key=lambda price: price.observed_at)
    notes: list[str] = []
    if not ordered:
        return _empty_features(notes=["No stored prices available."], window_minutes=window_minutes)

    latest = ordered[-1]
    latest_price = to_decimal(latest.price_usd)
    if latest_price is None:
        return _empty_features(
            notes=["Latest stored price is invalid."],
            window_minutes=window_minutes,
        )

    features: dict[str, Any] = {
        "price": latest_price,
        "window_minutes": window_minutes,
        "history_minutes": _history_minutes(ordered, latest),
    }
    for key, minutes in RETURN_WINDOWS.items():
        features[key] = _return_since(ordered, latest, minutes)
        if features[key] is None:
            notes.append(f"Insufficient history for {key}.")

    for key, minutes in VOL_WINDOWS.items():
        features[key] = _volatility(ordered, latest, minutes)
        if features[key] is None:
            notes.append(f"Insufficient history for {key}.")

    momentum = _momentum_score(features)
    features["momentum_score"] = momentum
    features["trend_direction"] = _trend_direction(momentum)
    features["notes"] = notes
    return _stringify_feature_values(features)


def _return_since(
    prices: list[CryptoPrice],
    latest: CryptoPrice,
    minutes: int,
) -> Decimal | None:
    latest_price = to_decimal(latest.price_usd)
    reference = _reference_price(prices, latest.observed_at - timedelta(minutes=minutes))
    reference_price = to_decimal(reference.price_usd) if reference is not None else None
    if latest_price is None or reference_price is None or reference_price == 0:
        return None
    return (latest_price / reference_price) - Decimal("1")


def _volatility(
    prices: list[CryptoPrice],
    latest: CryptoPrice,
    minutes: int,
) -> Decimal | None:
    start = latest.observed_at - timedelta(minutes=minutes)
    window = [price for price in prices if price.observed_at >= start]
    if len(window) < 3:
        return None
    returns: list[float] = []
    for previous, current in zip(window, window[1:], strict=False):
        previous_price = to_decimal(previous.price_usd)
        current_price = to_decimal(current.price_usd)
        if previous_price is None or current_price is None or previous_price == 0:
            continue
        returns.append(float((current_price / previous_price) - Decimal("1")))
    if len(returns) < 2:
        return None
    return Decimal(str(pstdev(returns)))


def _reference_price(
    prices: list[CryptoPrice],
    target_time: Any,
) -> CryptoPrice | None:
    candidates = [price for price in prices if price.observed_at <= target_time]
    return candidates[-1] if candidates else None


def _history_minutes(prices: list[CryptoPrice], latest: CryptoPrice) -> int:
    earliest = prices[0]
    return max(0, int((latest.observed_at - earliest.observed_at).total_seconds() // 60))


def _momentum_score(features: dict[str, Any]) -> Decimal | None:
    weighted: list[tuple[Decimal, Decimal]] = []
    for key, weight in (
        ("return_5m", Decimal("0.10")),
        ("return_15m", Decimal("0.15")),
        ("return_1h", Decimal("0.35")),
        ("return_4h", Decimal("0.25")),
        ("return_24h", Decimal("0.15")),
    ):
        value = to_decimal(features.get(key))
        if value is not None:
            weighted.append((value, weight))
    if not weighted:
        return None
    raw = sum((value * weight for value, weight in weighted), Decimal("0"))
    scaled = raw * Decimal("10")
    if scaled > Decimal("1"):
        return Decimal("1")
    if scaled < Decimal("-1"):
        return Decimal("-1")
    return scaled


def _trend_direction(momentum_score: Decimal | None) -> str:
    if momentum_score is None:
        return "UNKNOWN"
    if momentum_score > Decimal("0.01"):
        return "UP"
    if momentum_score < Decimal("-0.01"):
        return "DOWN"
    return "FLAT"


def _empty_features(*, notes: list[str], window_minutes: int) -> dict[str, Any]:
    features = {key: None for key in RETURN_WINDOWS | VOL_WINDOWS}
    features.update(
        {
            "price": None,
            "window_minutes": window_minutes,
            "history_minutes": 0,
            "momentum_score": None,
            "trend_direction": "UNKNOWN",
            "notes": notes,
        }
    )
    return features


def _stringify_feature_values(features: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in features.items():
        if isinstance(value, Decimal):
            result[key] = decimal_to_str(value)
        else:
            result[key] = value
    return result


def _quality_flags(features: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if features.get("price") is None:
        flags.append("missing_price")
    if features.get("momentum_score") is None:
        flags.append("missing_momentum")
    try:
        history_minutes = int(float(features.get("history_minutes") or 0))
    except (TypeError, ValueError):
        history_minutes = 0
    if history_minutes <= 0:
        flags.append("no_history")
    if any(features.get(key) is None for key in RETURN_WINDOWS):
        flags.append("incomplete_return_windows")
    if any(features.get(key) is None for key in VOL_WINDOWS):
        flags.append("incomplete_volatility_windows")
    return flags or ["ok"]
