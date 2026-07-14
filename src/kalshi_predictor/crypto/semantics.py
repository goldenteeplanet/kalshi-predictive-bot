from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.crypto.assets import (
    supported_crypto_asset,
    symbol_for_target_price,
    symbol_from_alias_text,
    symbol_from_event_ticker,
)
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import CryptoFeature, Market, MarketLeg
from kalshi_predictor.utils.time import parse_datetime

EXACT_LINK = "EXACT_LINK"
AMBIGUOUS = "AMBIGUOUS"
UNSUPPORTED = "UNSUPPORTED"
NOT_CRYPTO = "NOT_CRYPTO"

DEFAULT_FEATURE_MAX_AGE_MINUTES = 24 * 60
DEFAULT_FUTURE_SKEW_SECONDS = 30


@dataclass(frozen=True)
class CryptoComponentTerms:
    symbol: str
    side: str | None
    comparator: str
    threshold_value: str | None
    reference_price_source: str
    source_event: str | None = None
    source_market: str | None = None
    raw_text: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "comparator": self.comparator,
            "threshold_value": self.threshold_value,
            "reference_price_source": self.reference_price_source,
            "source_event": self.source_event,
            "source_market": self.source_market,
            "raw_text": self.raw_text,
        }


@dataclass(frozen=True)
class CryptoMarketTerms:
    ticker: str
    status: str
    symbol: str | None
    component_symbols: tuple[str, ...]
    components: tuple[CryptoComponentTerms, ...]
    reason_codes: tuple[str, ...]
    reference_price_source: str
    observation_time: str | None
    expiration_time: str | None
    settlement_time: str | None
    settlement_timezone: str
    settlement_rules: str | None
    series_ticker: str | None
    event_ticker: str | None
    market_type: str | None
    idempotency_key: str

    @property
    def is_crypto_candidate(self) -> bool:
        return self.status != NOT_CRYPTO

    @property
    def is_exact(self) -> bool:
        return self.status == EXACT_LINK

    def as_payload(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "status": self.status,
            "symbol": self.symbol,
            "component_symbols": list(self.component_symbols),
            "components": [component.as_payload() for component in self.components],
            "reason_codes": list(self.reason_codes),
            "reference_price_source": self.reference_price_source,
            "observation_time": self.observation_time,
            "expiration_time": self.expiration_time,
            "settlement_time": self.settlement_time,
            "settlement_timezone": self.settlement_timezone,
            "settlement_rules": self.settlement_rules,
            "series_ticker": self.series_ticker,
            "event_ticker": self.event_ticker,
            "market_type": self.market_type,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class FeatureCompatibility:
    ok: bool
    reason: str
    feature: CryptoFeature | None = None
    details: dict[str, Any] | None = None


def parse_crypto_market_terms(
    market: Market,
    *,
    legs: list[MarketLeg] | None = None,
) -> CryptoMarketTerms:
    raw = decode_json(market.raw_json)
    resolved_legs = legs or []
    text = _market_text(market, raw)
    source = _reference_price_source(text, raw)
    timezone = _settlement_timezone(text, raw)
    components = _components_from_custom_strike(raw, resolved_legs, source)
    if not components:
        components = _components_from_legs(resolved_legs, source)

    reason_codes: list[str] = []
    unsupported_prices = _unsupported_target_prices(resolved_legs)
    alias_symbol = _contextual_alias_symbol(text)
    event_symbol = symbol_from_event_ticker(market.event_ticker) or symbol_from_event_ticker(
        market.series_ticker
    )

    if components and unsupported_prices:
        symbols = sorted({component.symbol for component in components})
        reason_codes.append("ambiguous_supported_and_unsupported_target_price_legs")
        return _terms(
            market,
            status=AMBIGUOUS,
            symbol=None,
            components=components,
            reason_codes=reason_codes,
            reference_price_source=source,
            timezone=timezone,
            raw=raw,
            extra={"unsupported_target_prices": unsupported_prices, "supported_symbols": symbols},
        )
    if components:
        symbols = tuple(sorted({component.symbol for component in components}))
        return _terms(
            market,
            status=EXACT_LINK,
            symbol="+".join(symbols),
            components=tuple(components),
            reason_codes=("structured_target_price_terms",),
            reference_price_source=source,
            timezone=timezone,
            raw=raw,
        )
    if unsupported_prices:
        return _terms(
            market,
            status=UNSUPPORTED,
            symbol=None,
            components=(),
            reason_codes=("unsupported_target_price_asset",),
            reference_price_source=source,
            timezone=timezone,
            raw=raw,
            extra={"unsupported_target_prices": unsupported_prices},
        )

    symbol = alias_symbol or event_symbol
    if symbol is not None and supported_crypto_asset(symbol) is not None:
        component = CryptoComponentTerms(
            symbol=symbol,
            side=None,
            comparator=_text_comparator(text),
            threshold_value=_first_target_price_from_text(text),
            reference_price_source=source,
            source_event=market.event_ticker,
            source_market=market.ticker,
            raw_text=_compact(text),
        )
        return _terms(
            market,
            status=EXACT_LINK,
            symbol=symbol,
            components=(component,),
            reason_codes=("asset_alias_or_event_symbol",),
            reference_price_source=source,
            timezone=timezone,
            raw=raw,
        )

    if _looks_like_crypto(text):
        return _terms(
            market,
            status=AMBIGUOUS,
            symbol=None,
            components=(),
            reason_codes=("generic_crypto_without_supported_asset",),
            reference_price_source=source,
            timezone=timezone,
            raw=raw,
        )

    return _terms(
        market,
        status=NOT_CRYPTO,
        symbol=None,
        components=(),
        reason_codes=("no_crypto_semantics",),
        reference_price_source=source,
        timezone=timezone,
        raw=raw,
    )


def terms_from_link_payload(link_symbol: str, raw_json: str | None) -> CryptoMarketTerms | None:
    raw = decode_json(raw_json)
    structured = raw.get("structured_terms")
    if isinstance(structured, dict):
        return _terms_from_payload(structured)
    components = raw.get("components")
    if isinstance(components, list):
        rows = []
        for item in components:
            if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
                continue
            rows.append(
                CryptoComponentTerms(
                    symbol=str(item["symbol"]).upper(),
                    side=str(item.get("side") or "") or None,
                    comparator=str(item.get("comparator") or item.get("direction") or "UNKNOWN"),
                    threshold_value=(
                        str(item.get("threshold_value"))
                        if item.get("threshold_value") is not None
                        else None
                    ),
                    reference_price_source=str(
                        item.get("reference_price_source") or "unknown_public_reference"
                    ),
                    source_event=str(item.get("source_event") or "") or None,
                    source_market=str(item.get("source_market") or "") or None,
                    raw_text=str(item.get("raw_text") or "") or None,
                )
            )
        if rows:
            symbol = "+".join(sorted({row.symbol for row in rows}))
            return CryptoMarketTerms(
                ticker=str(raw.get("ticker") or ""),
                status=EXACT_LINK,
                symbol=symbol or link_symbol,
                component_symbols=tuple(sorted({row.symbol for row in rows})),
                components=tuple(rows),
                reason_codes=("legacy_link_components",),
                reference_price_source=rows[0].reference_price_source,
                observation_time=None,
                expiration_time=None,
                settlement_time=None,
                settlement_timezone="unknown",
                settlement_rules=None,
                series_ticker=None,
                event_ticker=None,
                market_type=None,
                idempotency_key=_stable_key(raw.get("ticker"), symbol, rows),
            )
    return None


def select_compatible_crypto_feature(
    session: Session,
    *,
    symbol: str,
    terms: CryptoMarketTerms,
    forecast_cutoff: Any,
    max_age_minutes: int = DEFAULT_FEATURE_MAX_AGE_MINUTES,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
) -> FeatureCompatibility:
    cutoff = parse_datetime(forecast_cutoff)
    if cutoff is None:
        return FeatureCompatibility(False, "invalid_forecast_cutoff")
    rows = list(
        session.scalars(
            select(CryptoFeature)
            .where(CryptoFeature.symbol == symbol.upper())
            .order_by(desc(CryptoFeature.generated_at), desc(CryptoFeature.id))
            .limit(25)
        )
    )
    if not rows:
        return FeatureCompatibility(False, "no_feature_at_or_before_cutoff")
    latest_reason = "no_compatible_feature"
    latest_details: dict[str, Any] = {}
    for feature in rows:
        compatibility = validate_crypto_feature(
            feature,
            terms=terms,
            forecast_cutoff=cutoff,
            max_age_minutes=max_age_minutes,
            future_skew_seconds=future_skew_seconds,
        )
        if compatibility.ok:
            return compatibility
        latest_reason = compatibility.reason
        latest_details = compatibility.details or {}
    return FeatureCompatibility(False, latest_reason, details=latest_details)


def validate_crypto_feature(
    feature: CryptoFeature | None,
    *,
    terms: CryptoMarketTerms,
    forecast_cutoff: Any,
    max_age_minutes: int = DEFAULT_FEATURE_MAX_AGE_MINUTES,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
) -> FeatureCompatibility:
    if feature is None:
        return FeatureCompatibility(False, "missing_feature")
    cutoff = parse_datetime(forecast_cutoff)
    generated_at = parse_datetime(feature.generated_at)
    if cutoff is None or generated_at is None:
        return FeatureCompatibility(False, "invalid_feature_or_cutoff_time")
    if feature.symbol.upper() not in {symbol.upper() for symbol in terms.component_symbols}:
        return FeatureCompatibility(
            False,
            "feature_symbol_does_not_match_market_terms",
            feature=feature,
            details={
                "feature_symbol": feature.symbol,
                "term_symbols": list(terms.component_symbols),
            },
        )
    upper_bound = cutoff + timedelta(seconds=future_skew_seconds)
    if generated_at > upper_bound:
        return FeatureCompatibility(
            False,
            "future_feature",
            feature=feature,
            details={"generated_at": generated_at.isoformat(), "cutoff": cutoff.isoformat()},
        )
    age_minutes = (cutoff - generated_at).total_seconds() / 60
    if age_minutes > max_age_minutes:
        return FeatureCompatibility(
            False,
            "stale_feature",
            feature=feature,
            details={"age_minutes": round(age_minutes, 2), "max_age_minutes": max_age_minutes},
        )
    settlement_time = parse_datetime(terms.settlement_time)
    if settlement_time is not None and generated_at > settlement_time + timedelta(
        seconds=future_skew_seconds
    ):
        return FeatureCompatibility(
            False,
            "post_settlement_feature",
            feature=feature,
            details={
                "generated_at": generated_at.isoformat(),
                "settlement_time": settlement_time.isoformat(),
            },
        )
    raw = decode_json(feature.raw_json)
    latest_source = parse_datetime(
        raw.get("source_latest_observed_at")
        or raw.get("latest_price_observed_at")
        or raw.get("source_timestamp")
    )
    if latest_source is not None and latest_source > upper_bound:
        return FeatureCompatibility(
            False,
            "future_source_timestamp",
            feature=feature,
            details={
                "source_latest_observed_at": latest_source.isoformat(),
                "cutoff": cutoff.isoformat(),
            },
        )
    if terms.reference_price_source == "coinbase" and feature.source not in {
        "coinbase",
        "stored_prices",
        "test",
    }:
        return FeatureCompatibility(
            False,
            "incompatible_reference_price_source",
            feature=feature,
            details={
                "required_source": terms.reference_price_source,
                "feature_source": feature.source,
            },
        )
    return FeatureCompatibility(
        True,
        "compatible",
        feature=feature,
        details={
            "feature_id": feature.id,
            "generated_at": generated_at.isoformat(),
            "cutoff": cutoff.isoformat(),
            "age_minutes": round(max(age_minutes, 0), 2),
            "feature_source": feature.source,
        },
    )


def _terms(
    market: Market,
    *,
    status: str,
    symbol: str | None,
    components: tuple[CryptoComponentTerms, ...] | list[CryptoComponentTerms],
    reason_codes: tuple[str, ...] | list[str],
    reference_price_source: str,
    timezone: str,
    raw: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> CryptoMarketTerms:
    rows = tuple(components)
    component_symbols = tuple(sorted({row.symbol for row in rows}))
    expiration = _time_value(
        market.expected_expiration_time,
        market.expiration_time,
        raw.get("expected_expiration_time"),
        raw.get("expiration_time"),
    )
    settlement = _time_value(market.settlement_ts, raw.get("settlement_ts"), raw.get("settled_at"))
    observation = _time_value(
        raw.get("observation_time"),
        raw.get("observation_end_time"),
        market.close_time,
    )
    payload = {
        "ticker": market.ticker,
        "symbol": symbol,
        "component_symbols": component_symbols,
        "status": status,
        "reason_codes": tuple(reason_codes),
        "expiration_time": expiration,
        "settlement_time": settlement,
        "observation_time": observation,
        "extra": extra or {},
    }
    return CryptoMarketTerms(
        ticker=market.ticker,
        status=status,
        symbol=symbol,
        component_symbols=component_symbols,
        components=rows,
        reason_codes=tuple(reason_codes),
        reference_price_source=reference_price_source,
        observation_time=observation,
        expiration_time=expiration,
        settlement_time=settlement,
        settlement_timezone=timezone,
        settlement_rules=_compact(
            " ".join(str(x or "") for x in (market.rules_primary, market.rules_secondary))
        ),
        series_ticker=market.series_ticker,
        event_ticker=market.event_ticker,
        market_type=market.market_type,
        idempotency_key=_stable_key(payload),
    )


def _terms_from_payload(payload: dict[str, Any]) -> CryptoMarketTerms:
    components = tuple(
        CryptoComponentTerms(
            symbol=str(item.get("symbol") or "").upper(),
            side=str(item.get("side") or "") or None,
            comparator=str(item.get("comparator") or "UNKNOWN"),
            threshold_value=str(item.get("threshold_value"))
            if item.get("threshold_value") is not None
            else None,
            reference_price_source=str(
                item.get("reference_price_source")
                or payload.get("reference_price_source")
                or "unknown"
            ),
            source_event=str(item.get("source_event") or "") or None,
            source_market=str(item.get("source_market") or "") or None,
            raw_text=str(item.get("raw_text") or "") or None,
        )
        for item in payload.get("components", [])
        if isinstance(item, dict) and item.get("symbol")
    )
    symbols = tuple(payload.get("component_symbols") or [item.symbol for item in components])
    return CryptoMarketTerms(
        ticker=str(payload.get("ticker") or ""),
        status=str(payload.get("status") or EXACT_LINK),
        symbol=str(payload.get("symbol") or "") or None,
        component_symbols=tuple(str(symbol).upper() for symbol in symbols),
        components=components,
        reason_codes=tuple(str(item) for item in payload.get("reason_codes", [])),
        reference_price_source=str(payload.get("reference_price_source") or "unknown"),
        observation_time=payload.get("observation_time"),
        expiration_time=payload.get("expiration_time"),
        settlement_time=payload.get("settlement_time"),
        settlement_timezone=str(payload.get("settlement_timezone") or "unknown"),
        settlement_rules=payload.get("settlement_rules"),
        series_ticker=payload.get("series_ticker"),
        event_ticker=payload.get("event_ticker"),
        market_type=payload.get("market_type"),
        idempotency_key=str(payload.get("idempotency_key") or _stable_key(payload)),
    )


def _components_from_custom_strike(
    raw: dict[str, Any],
    legs: list[MarketLeg],
    reference_price_source: str,
) -> tuple[CryptoComponentTerms, ...]:
    custom = raw.get("custom_strike")
    if not isinstance(custom, dict):
        return ()
    events = _csv_values(custom.get("Associated Events"))
    markets = _csv_values(custom.get("Associated Markets"))
    sides = _csv_values(custom.get("Associated Market Sides"))
    rows: list[CryptoComponentTerms] = []
    for index, event in enumerate(events):
        source_market = markets[index] if index < len(markets) else None
        symbol = symbol_from_event_ticker(event) or symbol_from_event_ticker(source_market)
        if symbol is None or supported_crypto_asset(symbol) is None:
            continue
        side = sides[index].upper() if index < len(sides) and sides[index] else None
        leg = legs[index] if index < len(legs) else None
        rows.append(
            CryptoComponentTerms(
                symbol=symbol,
                side=side,
                comparator=_leg_comparator(leg, side=side),
                threshold_value=leg.threshold_value if leg else None,
                reference_price_source=reference_price_source,
                source_event=event,
                source_market=source_market,
                raw_text=leg.raw_text if leg else None,
            )
        )
    return tuple(rows)


def _components_from_legs(
    legs: list[MarketLeg],
    reference_price_source: str,
) -> tuple[CryptoComponentTerms, ...]:
    rows: list[CryptoComponentTerms] = []
    for leg in legs:
        if leg.category != "crypto" or leg.unit != "USD" or leg.threshold_value is None:
            continue
        price = _decimal_or_none(leg.threshold_value)
        symbol = symbol_for_target_price(price) if price is not None else None
        if symbol is None or supported_crypto_asset(symbol) is None:
            continue
        rows.append(
            CryptoComponentTerms(
                symbol=symbol,
                side=leg.side,
                comparator=_leg_comparator(leg, side=leg.side),
                threshold_value=leg.threshold_value,
                reference_price_source=reference_price_source,
                raw_text=leg.raw_text,
            )
        )
    return tuple(rows)


def _unsupported_target_prices(legs: list[MarketLeg]) -> list[str]:
    prices: list[str] = []
    for leg in legs:
        if leg.category != "crypto" or leg.unit != "USD" or leg.threshold_value is None:
            continue
        parsed = _decimal_or_none(leg.threshold_value)
        if parsed is not None and symbol_for_target_price(parsed) is None:
            prices.append(str(parsed))
    return prices


def _leg_comparator(leg: MarketLeg | None, *, side: str | None) -> str:
    operator = str(getattr(leg, "operator", "") or "").upper()
    normalized_side = str(side or "").upper()
    if operator in {"BELOW", "AT_MOST"}:
        return "BELOW" if normalized_side != "NO" else "ABOVE"
    if operator in {"ABOVE", "AT_LEAST"}:
        return "ABOVE" if normalized_side != "NO" else "BELOW"
    if operator == "EQUALS":
        return "EQUALS"
    return "UNKNOWN"


def _text_comparator(text: str) -> str:
    normalized = text.lower()
    if re.search(r"\b(above|greater than|exceed|at or above|over)\b", normalized):
        return "ABOVE"
    if re.search(r"\b(below|less than|under|at or below)\b", normalized):
        return "BELOW"
    if re.search(r"\b(range|between)\b", normalized):
        return "RANGE"
    return "UNKNOWN"


def _first_target_price_from_text(text: str) -> str | None:
    match = re.search(r"\$\s*([-+]?\d[\d,]*(?:\.\d+)?)", text)
    return match.group(1).replace(",", "") if match else None


def _reference_price_source(text: str, raw: dict[str, Any]) -> str:
    raw_text = " ".join(
        str(raw.get(key) or "")
        for key in ("rules_primary", "rules_secondary", "settlement_source", "price_source")
    )
    normalized = f"{text} {raw_text}".lower()
    if "coinbase" in normalized:
        return "coinbase"
    if "coingecko" in normalized:
        return "coingecko"
    if "kraken" in normalized:
        return "kraken"
    if "binance" in normalized:
        return "binance"
    return "unknown_public_reference"


def _settlement_timezone(text: str, raw: dict[str, Any]) -> str:
    explicit = raw.get("settlement_timezone") or raw.get("timezone") or raw.get("time_zone")
    if explicit:
        return str(explicit)
    normalized = text.lower()
    if re.search(r"\b(et|eastern time|america/new_york)\b", normalized):
        return "America/New_York"
    if re.search(r"\b(utc|zulu)\b", normalized):
        return "UTC"
    return "unknown"


def _market_text(market: Market, raw: dict[str, Any]) -> str:
    parts = [
        market.ticker,
        market.title,
        market.subtitle,
        market.series_ticker,
        market.event_ticker,
        market.market_type,
        market.rules_primary,
        market.rules_secondary,
        raw.get("title"),
        raw.get("subtitle"),
        raw.get("series_title"),
        raw.get("event_title"),
        raw.get("rules_primary"),
        raw.get("rules_secondary"),
        raw.get("rules"),
        raw.get("category"),
        raw.get("tags"),
        _raw_text(raw),
    ]
    return " ".join(str(part or "") for part in parts)


def _looks_like_crypto(text: str) -> bool:
    normalized = text.lower()
    return bool(
        _contextual_alias_symbol(text)
        or re.search(r"\b(crypto|cryptocurrency|cryptocurrencies|digital asset)\b", normalized)
        or re.search(r"\btarget price\b", normalized)
    )


def _contextual_alias_symbol(text: str) -> str | None:
    symbol = symbol_from_alias_text(text)
    if symbol is None:
        return None
    normalized = text.lower()
    if re.search(
        r"\b(target price|crypto|cryptocurrency|cryptocurrencies|digital asset|"
        r"coinbase|coingecko|kraken|binance|"
        r"kxbtc|kxeth|kxsol|kxxrp|kxdoge|"
        r"btcusd|ethusd|solusd|xrpusd|dogeusd|"
        r"btc-usd|eth-usd|sol-usd|xrp-usd|doge-usd)\b",
        normalized,
    ):
        return symbol
    if re.search(r"\$\s*[-+]?\d", text):
        return symbol
    if re.search(
        r"\b(above|below|over|under|exceed|exceeds|greater than|less than|at or above|"
        r"at or below)\s+\$?\s*[-+]?\d",
        normalized,
    ):
        return symbol
    return None


def _time_value(*values: Any) -> str | None:
    for value in values:
        parsed = parse_datetime(value)
        if parsed is not None:
            return parsed.isoformat()
    return None


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _csv_values(value: object) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _raw_text(raw: object) -> str:
    try:
        return json.dumps(raw, sort_keys=True)
    except TypeError:
        return str(raw)


def _stable_key(*parts: Any) -> str:
    text = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
