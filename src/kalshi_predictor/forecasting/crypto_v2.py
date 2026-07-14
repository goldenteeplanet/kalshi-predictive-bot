import re
from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import (
    get_latest_crypto_link_for_ticker,
)
from kalshi_predictor.crypto.semantics import (
    EXACT_LINK,
    DEFAULT_FEATURE_MAX_AGE_MINUTES,
    DEFAULT_FUTURE_SKEW_SECONDS,
    CryptoMarketTerms,
    FeatureCompatibility,
    parse_crypto_market_terms,
    select_compatible_crypto_feature,
    terms_from_link_payload,
    validate_crypto_feature,
)
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import CryptoFeature, Market, MarketLeg, MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.skip_log import log_forecast_skip
from kalshi_predictor.utils.decimals import midpoint, to_decimal
from kalshi_predictor.utils.time import parse_datetime


class CryptoV2Forecaster:
    model_name = "crypto_v2"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._feature_rows_by_symbol: dict[str, list[CryptoFeature]] | None = None

    def begin_forecast_run(self) -> None:
        self._feature_rows_by_symbol = {}

    def end_forecast_run(self) -> None:
        self._feature_rows_by_symbol = None

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        link = get_latest_crypto_link_for_ticker(session, snapshot.ticker)
        if link is None:
            _skip(session, snapshot, "no crypto market link", available={"snapshot": True})
            return None
        link_confidence = to_decimal(link.confidence)
        if link_confidence is None or link_confidence < self.settings.crypto_v2_min_link_confidence:
            _skip(
                session,
                snapshot,
                "crypto market link confidence too low",
                available={"link": True, "confidence": link.confidence},
            )
            return None

        terms = _crypto_terms_for_snapshot(session, snapshot, link.symbol, link.raw_json)
        if terms is None or terms.status != EXACT_LINK or not terms.component_symbols:
            _skip(
                session,
                snapshot,
                "crypto market terms ambiguous or unsupported",
                available={
                    "symbol": link.symbol,
                    "terms_status": getattr(terms, "status", None),
                    "reason_codes": list(getattr(terms, "reason_codes", ())),
                },
            )
            return None
        non_crypto_legs = _non_crypto_component_legs(session, snapshot.ticker)
        if non_crypto_legs:
            _skip(
                session,
                snapshot,
                "crypto market includes non-crypto component legs",
                available={
                    "symbol": link.symbol,
                    "non_crypto_leg_categories": sorted(
                        {str(leg.category) for leg in non_crypto_legs}
                    ),
                    "non_crypto_leg_count": len(non_crypto_legs),
                },
            )
            return None

        components = _link_components(link.symbol, link.raw_json, terms=terms)
        component_rows = _component_feature_rows(
            session,
            components,
            terms=terms,
            snapshot=snapshot,
            feature_rows_by_symbol=self._feature_rows_by_symbol,
        )
        missing = [item["symbol"] for item in component_rows if item["features"] is None]
        if missing:
            reason = (
                "no crypto features"
                if len(component_rows) == 1
                else "no crypto features for linked component"
            )
            _skip(
                session,
                snapshot,
                _feature_missing_reason(component_rows, default_reason=reason),
                available={
                    "symbol": link.symbol,
                    "missing_components": missing,
                    "component_reasons": {
                        item["symbol"]: item.get("feature_reason") for item in component_rows
                    },
                },
            )
            return None
        missing_momentum = [
            item["symbol"]
            for item in component_rows
            if getattr(item["features"], "momentum_score", None) is None
        ]
        if missing_momentum:
            _skip(
                session,
                snapshot,
                "no crypto momentum score",
                available={"symbol": link.symbol, "missing_components": missing_momentum},
            )
            return None
        history_minutes = _minimum_component_history(component_rows)
        if history_minutes is None or history_minutes < self.settings.crypto_v2_min_history_minutes:
            _skip(
                session,
                snapshot,
                "crypto features have insufficient history",
                available={"symbol": link.symbol, "history_minutes": history_minutes},
            )
            return None

        market_mid = _market_midpoint(snapshot)
        if market_mid is None:
            _skip(
                session,
                snapshot,
                "no market midpoint",
                available={
                    "best_yes_bid": snapshot.best_yes_bid,
                    "best_yes_ask": snapshot.best_yes_ask,
                    "last_price": snapshot.last_price_dollars,
                },
            )
            return None

        raw_market = decode_json(snapshot.raw_market_json)
        title = str(raw_market.get("title") or "")
        direction = detect_market_direction(title)
        momentum_score = _link_momentum_score(component_rows, fallback_direction=direction)
        if momentum_score is None:
            _skip(
                session,
                snapshot,
                "no crypto momentum score",
                available={"symbol": link.symbol},
            )
            return None

        direction_detected = "MULTI_COMPONENT" if len(components) > 1 else direction
        adjustment = momentum_score * self.settings.crypto_v2_max_adjustment
        final_probability = _clamp_probability(market_mid + adjustment)

        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=final_probability,
            market_mid_probability=market_mid,
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "symbol": link.symbol,
                "component_symbols": [item["symbol"] for item in component_rows],
                "linked_confidence": link.confidence,
                "structured_terms": terms.as_payload(),
                "forecast_cutoff": snapshot.captured_at.isoformat(),
                "point_in_time_validation": {
                    item["symbol"]: item.get("feature_compatibility")
                    for item in component_rows
                },
                "title": title,
                "direction_detected": direction_detected,
                "market_mid": str(market_mid),
                "momentum_score": str(momentum_score),
                "history_minutes": history_minutes,
                "adjustment": str(adjustment),
                "final_probability": str(final_probability),
                "crypto_feature_id": _primary_feature_id(component_rows),
                "component_feature_ids": _component_feature_ids(component_rows),
                "feature_snapshot_id": _primary_feature_id(component_rows),
            },
            notes=(
                "crypto_v2 midpoint plus bounded momentum adjustment using "
                "point-in-time crypto features."
            ),
        )


def detect_market_direction(text: str) -> str:
    normalized = text.lower()
    if re.search(r"\b(above|greater than|exceed|at or above|over)\b", normalized):
        return "ABOVE"
    if re.search(r"\b(below|less than|under|at or below)\b", normalized):
        return "BELOW"
    return "UNKNOWN"


def _market_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    if yes_bid is not None and yes_ask is not None:
        return midpoint(yes_bid, yes_ask)
    return to_decimal(snapshot.last_price_dollars)


def _feature_history_minutes(raw_json: str | None) -> int | None:
    raw_features = decode_json(raw_json)
    value = raw_features.get("history_minutes")
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _adjustment(
    *,
    direction: str,
    momentum_score: Decimal,
    max_adjustment: Decimal,
) -> Decimal:
    if direction == "ABOVE":
        directional_momentum = momentum_score
    elif direction == "BELOW":
        directional_momentum = -momentum_score
    else:
        directional_momentum = Decimal("0")
    if directional_momentum > Decimal("1"):
        directional_momentum = Decimal("1")
    if directional_momentum < Decimal("-1"):
        directional_momentum = Decimal("-1")
    return directional_momentum * max_adjustment


def _crypto_terms_for_snapshot(
    session: Session,
    snapshot: MarketSnapshot,
    link_symbol: str,
    raw_json: str | None,
) -> CryptoMarketTerms | None:
    terms = terms_from_link_payload(link_symbol, raw_json)
    if terms is not None and terms.status == EXACT_LINK and terms.component_symbols:
        return terms
    market = session.get(Market, snapshot.ticker)
    if market is None:
        return terms
    legs = list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker == snapshot.ticker)
            .order_by(MarketLeg.leg_index)
        )
    )
    return parse_crypto_market_terms(market, legs=legs)


def _non_crypto_component_legs(session: Session, ticker: str) -> list[MarketLeg]:
    legs = list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker == ticker)
            .order_by(MarketLeg.leg_index)
        )
    )
    return [leg for leg in legs if str(leg.category).lower() != "crypto"]


def _link_components(
    symbol: str,
    raw_json: str | None,
    *,
    terms: CryptoMarketTerms | None = None,
) -> list[dict[str, object]]:
    if terms is not None and terms.components:
        return [
            {
                "symbol": component.symbol,
                "direction": component.comparator,
                "threshold_value": component.threshold_value,
                "reference_price_source": component.reference_price_source,
            }
            for component in terms.components
        ]
    raw = decode_json(raw_json)
    components = raw.get("components")
    if isinstance(components, list):
        rows = [
            item
            for item in components
            if isinstance(item, dict) and isinstance(item.get("symbol"), str)
        ]
        if rows:
            return rows
    symbols = raw.get("component_symbols")
    if isinstance(symbols, list):
        rows = [
            {"symbol": item, "direction": "UNKNOWN"}
            for item in symbols
            if isinstance(item, str)
        ]
        if rows:
            return rows
    return [{"symbol": item, "direction": "UNKNOWN"} for item in symbol.split("+") if item]


def _component_feature_rows(
    session: Session,
    components: list[dict[str, object]],
    *,
    terms: CryptoMarketTerms,
    snapshot: MarketSnapshot,
    feature_rows_by_symbol: dict[str, list[CryptoFeature]] | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for component in components:
        symbol = str(component.get("symbol") or "").upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if feature_rows_by_symbol is None:
            compatibility = select_compatible_crypto_feature(
                session,
                symbol=symbol,
                terms=terms,
                forecast_cutoff=snapshot.captured_at,
            )
        else:
            if symbol not in feature_rows_by_symbol:
                feature_rows_by_symbol[symbol] = _latest_crypto_feature_rows(session, symbol)
            compatibility = _select_compatible_crypto_feature_from_rows(
                feature_rows_by_symbol[symbol],
                terms=terms,
                forecast_cutoff=snapshot.captured_at,
            )
        rows.append(
            {
                **component,
                "symbol": symbol,
                "features": compatibility.feature if compatibility.ok else None,
                "feature_reason": compatibility.reason,
                "feature_compatibility": compatibility.details
                or {"ok": compatibility.ok, "reason": compatibility.reason},
            }
        )
    return rows


def _latest_crypto_feature_rows(session: Session, symbol: str) -> list[CryptoFeature]:
    return list(
        session.scalars(
            select(CryptoFeature)
            .where(CryptoFeature.symbol == symbol.upper())
            .order_by(desc(CryptoFeature.generated_at), desc(CryptoFeature.id))
            .limit(25)
        )
    )


def _select_compatible_crypto_feature_from_rows(
    rows: list[CryptoFeature],
    *,
    terms: CryptoMarketTerms,
    forecast_cutoff: object,
    max_age_minutes: int = DEFAULT_FEATURE_MAX_AGE_MINUTES,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
) -> FeatureCompatibility:
    cutoff = parse_datetime(forecast_cutoff)
    if cutoff is None:
        return FeatureCompatibility(False, "invalid_forecast_cutoff")
    if not rows:
        return FeatureCompatibility(False, "no_feature_at_or_before_cutoff")
    latest_reason = "no_compatible_feature"
    latest_details: dict[str, object] = {}
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


def _feature_missing_reason(
    component_rows: list[dict[str, object]],
    *,
    default_reason: str,
) -> str:
    reasons = {
        str(row.get("feature_reason") or "")
        for row in component_rows
        if row.get("features") is None and row.get("feature_reason")
    }
    if reasons == {"no_feature_at_or_before_cutoff"}:
        return default_reason
    if len(reasons) == 1:
        return next(iter(reasons)).replace("_", " ")
    return default_reason


def _link_momentum_score(
    component_rows: list[dict[str, object]],
    *,
    fallback_direction: str,
) -> Decimal | None:
    values: list[Decimal] = []
    for row in component_rows:
        features = row.get("features")
        momentum = to_decimal(getattr(features, "momentum_score", None))
        if momentum is None:
            return None
        direction = str(row.get("direction") or "").upper()
        if not direction or direction == "UNKNOWN":
            direction = fallback_direction
        values.append(_signed_momentum(momentum, direction))
    if not values:
        return None
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(Decimal("0.0001"))


def _signed_momentum(momentum: Decimal, direction: str) -> Decimal:
    normalized = direction.upper()
    if normalized == "BELOW":
        return -momentum
    if normalized == "ABOVE":
        return momentum
    return Decimal("0")


def _minimum_component_history(component_rows: list[dict[str, object]]) -> int | None:
    values = [
        _feature_history_minutes(getattr(row.get("features"), "raw_json", None))
        for row in component_rows
    ]
    parsed = [value for value in values if value is not None]
    if len(parsed) != len(component_rows):
        return None
    return min(parsed) if parsed else None


def _primary_feature_id(component_rows: list[dict[str, object]]) -> int | None:
    if not component_rows:
        return None
    return getattr(component_rows[0].get("features"), "id", None)


def _component_feature_ids(component_rows: list[dict[str, object]]) -> dict[str, int | None]:
    return {
        str(row["symbol"]): getattr(row.get("features"), "id", None)
        for row in component_rows
    }


def _clamp_probability(value: Decimal) -> Decimal:
    if value < Decimal("0.01"):
        return Decimal("0.01")
    if value > Decimal("0.99"):
        return Decimal("0.99")
    return value


def _skip(
    session: Session,
    snapshot: MarketSnapshot,
    reason: str,
    *,
    available: dict[str, object],
) -> None:
    log_forecast_skip(
        session,
        model_name=CryptoV2Forecaster.model_name,
        ticker=snapshot.ticker,
        reason=reason,
        required_data=["crypto market link", "crypto features", "market midpoint"],
        available_data=available,
    )
