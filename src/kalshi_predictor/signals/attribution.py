from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    EconomicFeature,
    EconomicMarketLink,
    Forecast,
    MarketRanking,
    MarketSnapshot,
    MicrostructureSignal,
    NewsSignal,
    PaperOrder,
    SignalEvent,
    SignalForecast,
    SignalTrade,
    SportsSignal,
    WeatherFeature,
    WeatherMarketLink,
)
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.signals.signal_types import (
    CRYPTO_SIGNAL,
    ECONOMIC_SIGNAL,
    ENSEMBLE_AGREEMENT_SIGNAL,
    FALLBACK_SIGNAL,
    FRESH_DATA_SIGNAL,
    LIQUIDITY_SIGNAL,
    MARKET_DIVERGENCE_SIGNAL,
    META_SELECTION_SIGNAL,
    MODEL_DISAGREEMENT_SIGNAL,
    MODEL_TRUST_SIGNAL,
    MOMENTUM_SIGNAL,
    OPPORTUNITY_SCORE_SIGNAL,
    SPECIALIZED_MODEL_ADVANTAGE_SIGNAL,
    SPORTS_SIGNAL,
    SPREAD_COMPRESSION_SIGNAL,
    WEATHER_SIGNAL,
)
from kalshi_predictor.signals.skip_log import log_signal_skip
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class ActiveSignal:
    signal_name: str
    category: str
    signal_strength: Decimal
    signal_value: str | None
    signal_direction: str | None
    confidence: Decimal
    contribution_score: Decimal
    description: str

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "signal_name": self.signal_name,
            "category": self.category,
            "signal_strength": decimal_to_str(self.signal_strength),
            "signal_value": self.signal_value,
            "signal_direction": self.signal_direction,
            "confidence": decimal_to_str(self.confidence),
            "contribution_score": decimal_to_str(self.contribution_score),
            "description": self.description,
        }

    def as_badge(self) -> dict[str, str]:
        return {
            "name": self.signal_name,
            "category": self.category,
            "confidence": decimal_to_str(self.confidence) or "0",
            "direction": self.signal_direction or "neutral",
        }


def extract_active_signals(
    session: Session,
    *,
    ticker: str,
    model_name: str,
    forecast: Forecast | None = None,
    ranking: MarketRanking | None = None,
    snapshot: MarketSnapshot | None = None,
    log_skips: bool = True,
) -> list[ActiveSignal]:
    forecast = forecast or _latest_forecast(session, ticker=ticker, model_name=model_name)
    ranking = ranking or _latest_ranking(session, ticker=ticker, model_name=model_name)
    snapshot = snapshot or _latest_snapshot(session, ticker=ticker)
    crypto_link, crypto_feature = _latest_crypto_context(session, ticker=ticker)
    economic_link, economic_feature = _latest_economic_context(session, ticker=ticker)
    weather_link, weather_feature = _latest_weather_context(session, ticker=ticker)
    components = _component_forecasts(forecast)

    signals: list[ActiveSignal] = []
    signals.extend(_meta_signals(forecast, model_name=model_name))
    if model_name.startswith("weather") or weather_feature is not None:
        confidence = _bounded_decimal(
            weather_feature.weather_confidence_score if weather_feature else None,
            fallback=Decimal("65"),
        )
        signals.append(
            _signal(
                WEATHER_SIGNAL,
                "Weather",
                confidence,
                weather_link.weather_metric if weather_link else model_name,
                _direction_from_probability(forecast.yes_probability if forecast else None),
                confidence,
                "Weather-linked evidence contributed to this forecast.",
            )
        )

    if crypto_link is not None and crypto_feature is not None and snapshot is not None:
        confidence = _bounded_decimal(crypto_feature.momentum_score, fallback=Decimal("65"))
        signals.append(
            _signal(
                CRYPTO_SIGNAL,
                "Crypto",
                confidence,
                crypto_feature.return_24h,
                crypto_feature.trend_direction,
                confidence,
                "Crypto-linked evidence contributed to this forecast.",
            )
        )
    elif log_skips and (model_name.startswith("crypto") or crypto_link is not None):
        _log_crypto_skip(
            session,
            ticker=ticker,
            link=crypto_link,
            feature=crypto_feature,
            snapshot=snapshot,
        )

    if economic_link is not None and economic_feature is not None and snapshot is not None:
        confidence = _bounded_decimal(economic_feature.confidence_score, fallback=Decimal("65"))
        signals.append(
            _signal(
                ECONOMIC_SIGNAL,
                "Economic",
                confidence,
                economic_feature.surprise_score or economic_link.event_key,
                economic_feature.direction,
                confidence,
                "Economic feature evidence contributed to this forecast.",
            )
        )
    elif log_skips and (
        model_name.startswith("economic")
        or _has_economic_features(forecast)
        or economic_link is not None
    ):
        _log_economic_skip(
            session,
            ticker=ticker,
            link=economic_link,
            feature=economic_feature,
            snapshot=snapshot,
        )

    for news_signal in _latest_news_signals(session, ticker=ticker):
        strength = _bounded_decimal(news_signal.signal_strength, fallback=Decimal("60"))
        confidence = _bounded_decimal(news_signal.confidence, fallback=Decimal("60"))
        signals.append(
            _signal(
                news_signal.signal_name,
                "News",
                strength,
                news_signal.explanation,
                news_signal.signal_direction,
                confidence,
                news_signal.explanation,
            )
        )

    for sports_signal in _latest_sports_signals(session, ticker=ticker):
        strength = _bounded_decimal(sports_signal.signal_strength, fallback=Decimal("60"))
        confidence = _bounded_decimal(sports_signal.confidence, fallback=Decimal("60"))
        signals.append(
            _signal(
                sports_signal.signal_name or SPORTS_SIGNAL,
                "Sports",
                strength,
                sports_signal.explanation,
                sports_signal.signal_direction,
                confidence,
                sports_signal.explanation,
            )
        )

    for micro_signal in _latest_microstructure_signals(session, ticker=ticker):
        strength = _bounded_decimal(micro_signal.signal_strength, fallback=Decimal("60"))
        confidence = _bounded_decimal(micro_signal.confidence, fallback=Decimal("60"))
        signals.append(
            _signal(
                micro_signal.signal_name,
                "Microstructure",
                strength,
                micro_signal.explanation,
                micro_signal.signal_direction,
                confidence,
                micro_signal.explanation,
            )
        )

    divergence = _model_market_divergence(forecast=forecast, ranking=ranking, snapshot=snapshot)
    if divergence is not None and abs(divergence) >= Decimal("0.05"):
        signals.append(
            _signal(
                MARKET_DIVERGENCE_SIGNAL,
                "Market",
                min(abs(divergence) * Decimal("1000"), Decimal("100")),
                decimal_to_str(divergence),
                "positive" if divergence > 0 else "negative",
                Decimal("75"),
                "Model probability diverges from market-implied price.",
            )
        )

    liquidity_score = _liquidity_score(ranking=ranking, snapshot=snapshot)
    if liquidity_score is not None and liquidity_score >= Decimal("60"):
        signals.append(
            _signal(
                LIQUIDITY_SIGNAL,
                "Market Quality",
                liquidity_score,
                decimal_to_str(liquidity_score),
                "supportive",
                liquidity_score,
                "Liquidity is strong enough for paper/demo review.",
            )
        )

    spread = _spread(ranking=ranking, snapshot=snapshot)
    if spread is not None and spread <= Decimal("0.05"):
        strength = max(Decimal("0"), Decimal("100") - spread * Decimal("1000"))
        signals.append(
            _signal(
                SPREAD_COMPRESSION_SIGNAL,
                "Market Quality",
                strength,
                decimal_to_str(spread),
                "tight",
                Decimal("70"),
                "Spread is compressed enough to preserve more of the edge.",
            )
        )

    momentum = _momentum_strength(crypto_feature=crypto_feature, weather_feature=weather_feature)
    if momentum is not None and momentum >= Decimal("50"):
        signals.append(
            _signal(
                MOMENTUM_SIGNAL,
                "Momentum",
                momentum,
                decimal_to_str(momentum),
                "supportive",
                momentum,
                "Recent linked features show directional momentum.",
            )
        )

    agreement = _ensemble_agreement(components)
    if model_name.startswith("ensemble") or agreement is not None:
        strength = agreement or Decimal("60")
        signals.append(
            _signal(
                ENSEMBLE_AGREEMENT_SIGNAL,
                "Model",
                strength,
                f"{len(components)} components" if components else model_name,
                "agreement",
                strength,
                "Multiple model components agree on direction.",
            )
        )

    opportunity_score = to_decimal(ranking.opportunity_score if ranking else None)
    if opportunity_score is not None and opportunity_score >= Decimal("70"):
        signals.append(
            _signal(
                OPPORTUNITY_SCORE_SIGNAL,
                "Opportunity",
                opportunity_score,
                decimal_to_str(opportunity_score),
                "supportive",
                opportunity_score,
                "Opportunity score is high enough for review.",
            )
        )

    freshness = _freshness_score(snapshot)
    if freshness is not None and freshness >= Decimal("60"):
        signals.append(
            _signal(
                FRESH_DATA_SIGNAL,
                "Data Quality",
                freshness,
                decimal_to_str(freshness),
                "fresh",
                freshness,
                "Latest market snapshot is fresh.",
            )
        )

    signals.sort(key=lambda signal: signal.contribution_score, reverse=True)
    return signals


def signal_badges_for_opportunity(
    session: Session,
    *,
    ticker: str,
    model_name: str,
    limit: int = 3,
) -> list[dict[str, str]]:
    return [
        signal.as_badge()
        for signal in extract_active_signals(
            session,
            ticker=ticker,
            model_name=model_name,
            log_skips=False,
        )[:limit]
    ]


def attribute_forecast_signals(
    session: Session,
    forecast: Forecast,
    *,
    snapshot: MarketSnapshot | None = None,
    ensure_builtin: bool = True,
) -> list[SignalForecast]:
    if forecast.id is None:
        session.flush()
    if ensure_builtin:
        ensure_builtin_signals(session)
    active = extract_active_signals(
        session,
        ticker=forecast.ticker,
        model_name=forecast.model_name,
        forecast=forecast,
        snapshot=snapshot,
    )
    rows: list[SignalForecast] = []
    for signal in active:
        _record_signal_event(
            session,
            ticker=forecast.ticker,
            model_name=forecast.model_name,
            signal=signal,
            source="forecast",
            source_id=forecast.id,
        )
        row = _link_signal_forecast(session, forecast_id=int(forecast.id), signal=signal)
        if row is not None:
            rows.append(row)
    return rows


def attribute_paper_order_signals(session: Session, order: PaperOrder) -> list[SignalTrade]:
    if order.id is None:
        session.flush()
    ensure_builtin_signals(session)
    active = _signals_from_forecast_links(session, order.forecast_id)
    if not active:
        active = extract_active_signals(
            session,
            ticker=order.ticker,
            model_name=order.model_name,
        )
    rows: list[SignalTrade] = []
    for signal in active:
        _record_signal_event(
            session,
            ticker=order.ticker,
            model_name=order.model_name,
            signal=signal,
            source="paper_order",
            source_id=order.id,
        )
        row = _link_signal_trade(session, paper_order_id=int(order.id), signal=signal)
        if row is not None:
            rows.append(row)
    return rows


def _signals_from_forecast_links(
    session: Session,
    forecast_id: int | None,
) -> list[ActiveSignal]:
    if forecast_id is None:
        return []
    links = list(
        session.scalars(
            select(SignalForecast)
            .where(SignalForecast.forecast_id == forecast_id)
            .order_by(desc(SignalForecast.contribution_score), desc(SignalForecast.id))
        )
    )
    active = []
    for link in links:
        contribution = to_decimal(link.contribution_score) or Decimal("50")
        active.append(
            _signal(
                link.signal_name,
                "Attributed",
                contribution,
                "forecast attribution",
                "active",
                contribution,
                "Signal was active when the linked forecast was generated.",
            )
        )
    return active


def _meta_signals(forecast: Forecast | None, *, model_name: str) -> list[ActiveSignal]:
    if forecast is None or not model_name.startswith("meta_"):
        return []
    feature_json = decode_json(forecast.feature_json)
    selected = str(feature_json.get("selected_model") or "selected model")
    trust = _bounded_decimal(feature_json.get("selected_trust_score"), fallback=Decimal("50"))
    disagreement = _bounded_decimal(
        feature_json.get("model_disagreement_score"),
        fallback=Decimal("0"),
    )
    signals = [
        _signal(
            META_SELECTION_SIGNAL,
            "Meta Model",
            trust,
            selected,
            "selected",
            trust,
            f"Meta model selected {selected}.",
        )
    ]
    if trust >= Decimal("70"):
        signals.append(
            _signal(
                MODEL_TRUST_SIGNAL,
                "Meta Model",
                trust,
                selected,
                "trusted",
                trust,
                f"{selected} has a high meta trust score.",
            )
        )
    if disagreement >= Decimal("20"):
        signals.append(
            _signal(
                MODEL_DISAGREEMENT_SIGNAL,
                "Meta Model",
                disagreement,
                str(feature_json.get("model_disagreement_score")),
                "disagreement",
                Decimal("70"),
                "Candidate model probabilities materially disagree.",
            )
        )
    if feature_json.get("fallback_model_name"):
        signals.append(
            _signal(
                FALLBACK_SIGNAL,
                "Meta Model",
                Decimal("70"),
                str(feature_json.get("fallback_model_name")),
                "fallback",
                Decimal("70"),
                "Meta model used fallback logic.",
            )
        )
    if selected not in {"market_implied_v1", "ensemble_v1", "ensemble_v2"}:
        signals.append(
            _signal(
                SPECIALIZED_MODEL_ADVANTAGE_SIGNAL,
                "Meta Model",
                trust,
                selected,
                "specialized",
                trust,
                f"{selected} has specialized support.",
            )
        )
    return signals


def _latest_microstructure_signals(
    session: Session,
    *,
    ticker: str,
    limit: int = 5,
) -> list[MicrostructureSignal]:
    return list(
        session.scalars(
            select(MicrostructureSignal)
            .where(MicrostructureSignal.ticker == ticker)
            .order_by(desc(MicrostructureSignal.created_at), desc(MicrostructureSignal.id))
            .limit(limit)
        )
    )


def _record_signal_event(
    session: Session,
    *,
    ticker: str,
    model_name: str,
    signal: ActiveSignal,
    source: str,
    source_id: int | None,
) -> SignalEvent:
    row = SignalEvent(
        created_at=utc_now(),
        ticker=ticker,
        signal_name=signal.signal_name,
        model_name=model_name,
        signal_strength=decimal_to_str(signal.signal_strength) or "0",
        signal_value=signal.signal_value,
        signal_direction=signal.signal_direction,
        confidence=decimal_to_str(signal.confidence) or "0",
        raw_json=encode_json(
            {
                **signal.as_event_payload(),
                "source": source,
                "source_id": source_id,
            }
        ),
    )
    session.add(row)
    return row


def _link_signal_forecast(
    session: Session,
    *,
    forecast_id: int,
    signal: ActiveSignal,
) -> SignalForecast | None:
    existing = _pending_signal_forecast(session, forecast_id, signal.signal_name) or session.scalar(
        select(SignalForecast).where(
            SignalForecast.forecast_id == forecast_id,
            SignalForecast.signal_name == signal.signal_name,
        )
    )
    if existing is not None:
        return None
    row = SignalForecast(
        created_at=utc_now(),
        forecast_id=forecast_id,
        signal_name=signal.signal_name,
        contribution_score=decimal_to_str(signal.contribution_score) or "0",
    )
    session.add(row)
    return row


def _link_signal_trade(
    session: Session,
    *,
    paper_order_id: int,
    signal: ActiveSignal,
) -> SignalTrade | None:
    existing = _pending_signal_trade(session, paper_order_id, signal.signal_name) or session.scalar(
        select(SignalTrade).where(
            SignalTrade.paper_order_id == paper_order_id,
            SignalTrade.signal_name == signal.signal_name,
        )
    )
    if existing is not None:
        return None
    row = SignalTrade(
        created_at=utc_now(),
        paper_order_id=paper_order_id,
        signal_name=signal.signal_name,
        contribution_score=decimal_to_str(signal.contribution_score) or "0",
    )
    session.add(row)
    return row


def _latest_forecast(
    session: Session,
    *,
    ticker: str,
    model_name: str,
) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == model_name)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
    )


def _latest_ranking(
    session: Session,
    *,
    ticker: str,
    model_name: str,
) -> MarketRanking | None:
    return session.scalar(
        select(MarketRanking)
        .where(MarketRanking.ticker == ticker, MarketRanking.forecast_model == model_name)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
    )


def _latest_snapshot(session: Session, *, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
    )


def _latest_crypto_context(
    session: Session,
    *,
    ticker: str,
) -> tuple[CryptoMarketLink | None, CryptoFeature | None]:
    link = session.scalar(
        select(CryptoMarketLink)
        .where(CryptoMarketLink.ticker == ticker)
        .order_by(desc(CryptoMarketLink.detected_at), desc(CryptoMarketLink.id))
    )
    if link is None:
        return None, None
    feature = session.scalar(
        select(CryptoFeature)
        .where(CryptoFeature.symbol == link.symbol)
        .order_by(desc(CryptoFeature.generated_at), desc(CryptoFeature.id))
    )
    return link, feature


def _latest_economic_context(
    session: Session,
    *,
    ticker: str,
) -> tuple[EconomicMarketLink | None, EconomicFeature | None]:
    link = session.scalar(
        select(EconomicMarketLink)
        .where(EconomicMarketLink.ticker == ticker)
        .order_by(desc(EconomicMarketLink.detected_at), desc(EconomicMarketLink.id))
    )
    if link is None:
        return None, None
    feature = session.scalar(
        select(EconomicFeature)
        .where(EconomicFeature.event_key == link.event_key)
        .order_by(desc(EconomicFeature.generated_at), desc(EconomicFeature.id))
    )
    return link, feature


def _latest_weather_context(
    session: Session,
    *,
    ticker: str,
) -> tuple[WeatherMarketLink | None, WeatherFeature | None]:
    link = session.scalar(
        select(WeatherMarketLink)
        .where(WeatherMarketLink.ticker == ticker)
        .order_by(desc(WeatherMarketLink.detected_at), desc(WeatherMarketLink.id))
    )
    if link is None:
        return None, None
    feature = session.scalar(
        select(WeatherFeature)
        .where(WeatherFeature.location_key == link.location_key)
        .order_by(desc(WeatherFeature.generated_at), desc(WeatherFeature.id))
    )
    return link, feature


def _latest_news_signals(
    session: Session,
    *,
    ticker: str,
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


def _latest_sports_signals(
    session: Session,
    *,
    ticker: str,
    limit: int = 3,
) -> list[SportsSignal]:
    return list(
        session.scalars(
            select(SportsSignal)
            .where(SportsSignal.ticker == ticker)
            .order_by(desc(SportsSignal.created_at), desc(SportsSignal.id))
            .limit(limit)
        )
    )


def _component_forecasts(forecast: Forecast | None) -> dict[str, Any]:
    feature_json = decode_json(forecast.feature_json if forecast else None)
    components = feature_json.get("component_forecasts") or feature_json.get("components") or {}
    return components if isinstance(components, dict) else {}


def _has_economic_features(forecast: Forecast | None) -> bool:
    feature_json = decode_json(forecast.feature_json if forecast else None)
    text = encode_json(feature_json).lower()
    return "economic" in text or "inflation" in text or "rate" in text


def _log_crypto_skip(
    session: Session,
    *,
    ticker: str,
    link: CryptoMarketLink | None,
    feature: CryptoFeature | None,
    snapshot: MarketSnapshot | None,
) -> None:
    if link is None:
        reason = "no crypto links"
    elif feature is None:
        reason = "no crypto features"
    elif snapshot is None:
        reason = "no crypto market snapshots"
    else:
        reason = "crypto signal did not meet activation requirements"
    log_signal_skip(
        session,
        signal_name=CRYPTO_SIGNAL,
        ticker=ticker,
        reason=reason,
        required_data=["crypto market links", "crypto_features", "latest market snapshot"],
        available_data={
            "crypto_link": link is not None,
            "crypto_feature": feature is not None,
            "market_snapshot": snapshot is not None,
        },
    )


def _log_economic_skip(
    session: Session,
    *,
    ticker: str,
    link: EconomicMarketLink | None,
    feature: EconomicFeature | None,
    snapshot: MarketSnapshot | None,
) -> None:
    if link is None:
        reason = "no economic links"
    elif feature is None:
        reason = "no economic features"
    elif snapshot is None:
        reason = "no economic market snapshots"
    else:
        reason = "economic signal did not meet activation requirements"
    log_signal_skip(
        session,
        signal_name=ECONOMIC_SIGNAL,
        ticker=ticker,
        reason=reason,
        required_data=["economic market links", "economic_features", "latest market snapshot"],
        available_data={
            "economic_link": link is not None,
            "economic_feature": feature is not None,
            "market_snapshot": snapshot is not None,
        },
    )


def _model_market_divergence(
    *,
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    snapshot: MarketSnapshot | None,
) -> Decimal | None:
    if ranking and ranking.estimated_edge is not None:
        return to_decimal(ranking.estimated_edge)
    probability = to_decimal(forecast.yes_probability if forecast else None)
    price = to_decimal(
        forecast.market_mid_probability if forecast else None
    ) or _market_price(snapshot)
    if probability is None or price is None:
        return None
    return probability - price


def _market_price(snapshot: MarketSnapshot | None) -> Decimal | None:
    if snapshot is None:
        return None
    return (
        to_decimal(snapshot.last_price_dollars)
        or to_decimal(snapshot.best_yes_ask)
        or to_decimal(snapshot.yes_ask_dollars)
    )


def _liquidity_score(
    *,
    ranking: MarketRanking | None,
    snapshot: MarketSnapshot | None,
) -> Decimal | None:
    score = to_decimal(ranking.liquidity_score if ranking else None)
    if score is not None:
        return score
    liquidity = to_decimal(snapshot.volume_fp if snapshot else None)
    if liquidity is None:
        return None
    return min(liquidity / Decimal("100"), Decimal("100"))


def _spread(
    *,
    ranking: MarketRanking | None,
    snapshot: MarketSnapshot | None,
) -> Decimal | None:
    return to_decimal(ranking.spread if ranking else None) or to_decimal(
        snapshot.spread if snapshot else None
    )


def _momentum_strength(
    *,
    crypto_feature: CryptoFeature | None,
    weather_feature: WeatherFeature | None,
) -> Decimal | None:
    crypto_score = to_decimal(crypto_feature.momentum_score if crypto_feature else None)
    if crypto_score is not None:
        return crypto_score
    return to_decimal(weather_feature.weather_confidence_score if weather_feature else None)


def _ensemble_agreement(components: dict[str, Any]) -> Decimal | None:
    if len(components) < 2:
        return None
    probabilities = [to_decimal(value) for value in components.values()]
    numeric = [value for value in probabilities if value is not None]
    if len(numeric) < 2:
        return None
    yes_votes = sum(1 for value in numeric if value >= Decimal("0.5"))
    no_votes = len(numeric) - yes_votes
    agreement = Decimal(max(yes_votes, no_votes)) / Decimal(len(numeric))
    return agreement * Decimal("100")


def _freshness_score(snapshot: MarketSnapshot | None) -> Decimal | None:
    if snapshot is None:
        return None
    captured_at = _aware(snapshot.captured_at)
    age_minutes = Decimal(str(max((utc_now() - captured_at).total_seconds(), 0))) / Decimal("60")
    if age_minutes > Decimal("60"):
        return Decimal("0")
    return max(Decimal("0"), Decimal("100") - age_minutes * Decimal("2"))


def _direction_from_probability(value: Any) -> str:
    probability = to_decimal(value)
    if probability is None:
        return "neutral"
    if probability >= Decimal("0.55"):
        return "yes"
    if probability <= Decimal("0.45"):
        return "no"
    return "neutral"


def _signal(
    signal_name: str,
    category: str,
    strength: Decimal,
    value: Any,
    direction: str | None,
    confidence: Decimal,
    description: str,
) -> ActiveSignal:
    bounded_strength = _clamp(strength)
    bounded_confidence = _clamp(confidence)
    contribution = _clamp((bounded_strength + bounded_confidence) / Decimal("2"))
    return ActiveSignal(
        signal_name=signal_name,
        category=category,
        signal_strength=bounded_strength,
        signal_value=None if value is None else str(value),
        signal_direction=direction,
        confidence=bounded_confidence,
        contribution_score=contribution,
        description=description,
    )


def _bounded_decimal(value: Any, *, fallback: Decimal) -> Decimal:
    decimal = to_decimal(value)
    if decimal is None:
        return fallback
    if decimal <= Decimal("1"):
        decimal *= Decimal("100")
    return _clamp(decimal)


def _clamp(value: Decimal) -> Decimal:
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("100"):
        return Decimal("100")
    return value


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _pending_signal_forecast(
    session: Session,
    forecast_id: int,
    signal_name: str,
) -> SignalForecast | None:
    for item in session.new:
        if (
            isinstance(item, SignalForecast)
            and item.forecast_id == forecast_id
            and item.signal_name == signal_name
        ):
            return item
    return None


def _pending_signal_trade(
    session: Session,
    paper_order_id: int,
    signal_name: str,
) -> SignalTrade | None:
    for item in session.new:
        if (
            isinstance(item, SignalTrade)
            and item.paper_order_id == paper_order_id
            and item.signal_name == signal_name
        ):
            return item
    return None
