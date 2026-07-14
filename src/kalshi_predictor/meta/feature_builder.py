from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    Forecast,
    Market,
    MarketSnapshot,
    MicrostructureFeature,
    ModelConfidenceScore,
    ModelLeaderboard,
    NewsFeature,
    SignalEvent,
    SportsFeature,
    WeatherFeature,
    WeatherMarketLink,
)
from kalshi_predictor.features.repository import (
    latest_feature_snapshot_for_ticker,
    snapshot_external_payload,
)
from kalshi_predictor.meta.repository import insert_meta_model_feature, row_to_dict
from kalshi_predictor.signals.attribution import extract_active_signals
from kalshi_predictor.tournament.ranking import classify_market_category
from kalshi_predictor.utils.decimals import decimal_to_str, midpoint, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

META_CANDIDATE_MODELS = (
    "market_implied_v1",
    "crypto_v2",
    "weather_v2",
    "news_v1",
    "economic_v2",
    "economic_v1",
    "cpi_v1",
    "jobs_v1",
    "fed_v1",
    "gdp_v1",
    "sports_v1",
    "mlb_v1",
    "nba_v1",
    "nfl_v1",
    "nhl_v1",
    "microstructure_v1",
    "ensemble_v1",
    "ensemble_v2",
)


@dataclass(frozen=True)
class MetaFeatureBuildSummary:
    markets_scanned: int
    features_inserted: int
    skipped: int


def build_meta_features(
    session: Session,
    *,
    model_scope: str = "all",
    limit: int = 100,
    ticker: str | None = None,
    persist: bool = True,
) -> MetaFeatureBuildSummary:
    snapshots = _snapshots(session, ticker=ticker, limit=limit)
    inserted = 0
    skipped = 0
    for snapshot in snapshots:
        feature = build_meta_features_for_ticker(
            session,
            ticker=snapshot.ticker,
            snapshot=snapshot,
            model_scope=model_scope,
            persist=persist,
        )
        if feature is None:
            skipped += 1
        else:
            inserted += int(persist)
    return MetaFeatureBuildSummary(
        markets_scanned=len(snapshots),
        features_inserted=inserted,
        skipped=skipped,
    )


def build_meta_features_for_ticker(
    session: Session,
    *,
    ticker: str,
    snapshot: MarketSnapshot | None = None,
    model_scope: str = "all",
    persist: bool = True,
) -> dict[str, Any] | None:
    snapshot = snapshot or _latest_snapshot(session, ticker)
    if snapshot is None:
        return None
    market = session.get(Market, ticker)
    model_names = _models_for_scope(model_scope)
    forecasts = _latest_forecasts(session, ticker=ticker, model_names=model_names)
    probabilities = _model_probabilities(forecasts)
    active_signals = _active_signals(session, ticker=ticker, snapshot=snapshot, forecasts=forecasts)
    category = _category(market, snapshot)
    raw_market = decode_json(snapshot.raw_market_json)
    time_to_close = _time_to_close_minutes(market, raw_market)
    liquidity_score = _liquidity_score(market, snapshot, raw_market)
    spread_score = _spread_score(snapshot)
    freshness_score = _freshness_score(snapshot.captured_at)
    disagreement = model_disagreement_score(probabilities)
    agreement = model_agreement_score(probabilities)
    specialized = _specialized_features(session, ticker=ticker)
    performance = _model_performance(session, category=category)
    payload = {
        "created_at": utc_now(),
        "ticker": ticker,
        "category": category,
        "market_type": (market.market_type if market else None) or raw_market.get("market_type"),
        "time_to_close_minutes": time_to_close,
        "liquidity_score": liquidity_score,
        "spread_score": spread_score,
        "data_freshness_score": freshness_score,
        "signal_count": len(active_signals),
        "active_signals": active_signals,
        "model_probabilities": probabilities,
        "model_disagreement_score": disagreement,
        "model_agreement_score": agreement,
        "model_recent_performance": performance["model_recent_performance"],
        "category_performance": performance["category_performance"],
        "microstructure_features": specialized["microstructure"],
        "news_features": specialized["news"],
        "economic_features": specialized["economic"],
        "sports_features": specialized["sports"],
        "crypto_features": specialized["crypto"],
        "weather_features": specialized["weather"],
    }
    payload["raw_json"] = {
        **payload,
        "forecast_ids": {name: forecast.id for name, forecast in forecasts.items()},
        "market_mid_probability": decimal_to_str(_market_midpoint(snapshot)),
        "distance_from_market": _distance_from_market(probabilities, snapshot),
    }
    if not persist:
        return payload
    return row_to_dict(insert_meta_model_feature(session, payload))


def model_disagreement_score(model_probabilities: dict[str, Any]) -> Decimal:
    probabilities = _numeric_probabilities(model_probabilities)
    if len(probabilities) < 2:
        return Decimal("0")
    return max(probabilities) - min(probabilities)


def model_agreement_score(model_probabilities: dict[str, Any]) -> Decimal:
    probabilities = _numeric_probabilities(model_probabilities)
    if not probabilities:
        return Decimal("0")
    yes_votes = sum(1 for value in probabilities if value >= Decimal("0.5"))
    no_votes = len(probabilities) - yes_votes
    return Decimal(max(yes_votes, no_votes)) / Decimal(len(probabilities)) * Decimal("100")


def _snapshots(
    session: Session,
    *,
    ticker: str | None,
    limit: int,
) -> list[MarketSnapshot]:
    statement = select(MarketSnapshot)
    if ticker:
        statement = statement.where(MarketSnapshot.ticker == ticker)
    rows = list(
        session.scalars(
            statement.order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        )
    )
    latest: list[MarketSnapshot] = []
    seen: set[str] = set()
    for row in rows:
        if row.ticker in seen:
            continue
        seen.add(row.ticker)
        latest.append(row)
        if len(latest) >= limit:
            break
    return latest


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_forecasts(
    session: Session,
    *,
    ticker: str,
    model_names: tuple[str, ...],
) -> dict[str, Forecast]:
    rows = list(
        session.scalars(
            select(Forecast)
            .where(Forecast.ticker == ticker, Forecast.model_name.in_(model_names))
            .order_by(Forecast.model_name, desc(Forecast.forecasted_at), desc(Forecast.id))
        )
    )
    latest: dict[str, Forecast] = {}
    for forecast in rows:
        latest.setdefault(forecast.model_name, forecast)
    return latest


def _models_for_scope(model_scope: str) -> tuple[str, ...]:
    if model_scope == "all":
        return META_CANDIDATE_MODELS
    requested = tuple(part.strip() for part in model_scope.split(",") if part.strip())
    return requested or META_CANDIDATE_MODELS


def _model_probabilities(forecasts: dict[str, Forecast]) -> dict[str, str]:
    return {
        model_name: decimal_to_str(to_decimal(forecast.yes_probability)) or "0"
        for model_name, forecast in forecasts.items()
        if to_decimal(forecast.yes_probability) is not None
    }


def _active_signals(
    session: Session,
    *,
    ticker: str,
    snapshot: MarketSnapshot,
    forecasts: dict[str, Forecast],
) -> list[dict[str, Any]]:
    preferred_name = "ensemble_v2" if "ensemble_v2" in forecasts else next(
        iter(forecasts.keys()),
        "market_implied_v1",
    )
    forecast = forecasts.get(preferred_name)
    active = extract_active_signals(
        session,
        ticker=ticker,
        model_name=preferred_name,
        forecast=forecast,
        snapshot=snapshot,
    )
    stored_events = list(
        session.scalars(
            select(SignalEvent)
            .where(SignalEvent.ticker == ticker)
            .order_by(desc(SignalEvent.created_at), desc(SignalEvent.id))
            .limit(10)
        )
    )
    rows = [signal.as_event_payload() for signal in active[:10]]
    rows.extend(
        {
            "signal_name": event.signal_name,
            "category": "Stored",
            "signal_strength": event.signal_strength,
            "signal_value": event.signal_value,
            "signal_direction": event.signal_direction,
            "confidence": event.confidence,
            "source": "signal_events",
        }
        for event in stored_events[:5]
    )
    return rows


def _category(market: Market | None, snapshot: MarketSnapshot) -> str:
    raw = decode_json(snapshot.raw_market_json)
    parts = (
        market.ticker if market else snapshot.ticker,
        market.title if market else raw.get("title"),
        market.subtitle if market else raw.get("subtitle"),
        market.series_ticker if market else raw.get("series_ticker"),
        market.event_ticker if market else raw.get("event_ticker"),
        market.rules_primary if market else raw.get("rules"),
    )
    return classify_market_category(" ".join(str(part or "") for part in parts))


def _time_to_close_minutes(
    market: Market | None,
    raw_market: dict[str, Any],
) -> Decimal | None:
    close_time = market.close_time if market else None
    close_time = close_time or parse_datetime(raw_market.get("close_time"))
    if close_time is None:
        return None
    close_time = _aware(close_time)
    minutes = Decimal(str((close_time - utc_now()).total_seconds())) / Decimal("60")
    return max(Decimal("0"), minutes)


def _liquidity_score(
    market: Market | None,
    snapshot: MarketSnapshot,
    raw_market: dict[str, Any],
) -> Decimal:
    liquidity = (
        to_decimal(market.liquidity_dollars if market else None)
        or to_decimal(raw_market.get("liquidity_dollars"))
        or to_decimal(snapshot.volume_fp)
        or to_decimal(snapshot.open_interest_fp)
        or Decimal("0")
    )
    return min(Decimal("100"), liquidity / Decimal("100"))


def _spread_score(snapshot: MarketSnapshot) -> Decimal:
    spread = to_decimal(snapshot.spread)
    if spread is None:
        yes_bid = to_decimal(snapshot.best_yes_bid)
        yes_ask = to_decimal(snapshot.best_yes_ask)
        if yes_bid is not None and yes_ask is not None:
            spread = yes_ask - yes_bid
    if spread is None:
        return Decimal("0")
    return max(Decimal("0"), Decimal("100") - spread * Decimal("1000"))


def _freshness_score(captured_at: datetime) -> Decimal:
    age = Decimal(str((_aware(utc_now()) - _aware(captured_at)).total_seconds()))
    minutes = max(Decimal("0"), age / Decimal("60"))
    return max(Decimal("0"), Decimal("100") - minutes * Decimal("2"))


def _specialized_features(session: Session, *, ticker: str) -> dict[str, dict[str, Any]]:
    return {
        "crypto": _crypto_payload(session, ticker),
        "weather": _weather_payload(session, ticker),
        "news": _latest_row_payload(session, NewsFeature, NewsFeature.ticker == ticker),
        "economic": _economic_payload(session, ticker),
        "sports": _latest_row_payload(session, SportsFeature, SportsFeature.ticker == ticker),
        "microstructure": _latest_row_payload(
            session,
            MicrostructureFeature,
            MicrostructureFeature.ticker == ticker,
        ),
    }


def _crypto_payload(session: Session, ticker: str) -> dict[str, Any]:
    link = session.scalar(
        select(CryptoMarketLink)
        .where(CryptoMarketLink.ticker == ticker)
        .order_by(desc(CryptoMarketLink.detected_at), desc(CryptoMarketLink.id))
        .limit(1)
    )
    if link is None:
        return {}
    feature = session.scalar(
        select(CryptoFeature)
        .where(CryptoFeature.symbol == link.symbol)
        .order_by(desc(CryptoFeature.generated_at), desc(CryptoFeature.id))
        .limit(1)
    )
    return {"link": _row_payload(link), "feature": _row_payload(feature)}


def _weather_payload(session: Session, ticker: str) -> dict[str, Any]:
    link = session.scalar(
        select(WeatherMarketLink)
        .where(WeatherMarketLink.ticker == ticker)
        .order_by(desc(WeatherMarketLink.detected_at), desc(WeatherMarketLink.id))
        .limit(1)
    )
    if link is None:
        return {}
    feature = session.scalar(
        select(WeatherFeature)
        .where(WeatherFeature.location_key == link.location_key)
        .order_by(desc(WeatherFeature.generated_at), desc(WeatherFeature.id))
        .limit(1)
    )
    return {"link": _row_payload(link), "feature": _row_payload(feature)}


def _economic_payload(session: Session, ticker: str) -> dict[str, Any]:
    snapshot = latest_feature_snapshot_for_ticker(session, ticker)
    external = snapshot_external_payload(snapshot)
    value = external.get("economic")
    return value if isinstance(value, dict) else {}


def _latest_row_payload(session: Session, table: Any, criterion: Any) -> dict[str, Any]:
    row = session.scalar(select(table).where(criterion).order_by(desc(table.id)).limit(1))
    return _row_payload(row)


def _row_payload(row: Any | None) -> dict[str, Any]:
    if row is None:
        return {}
    payload: dict[str, Any] = {}
    for key in row.__mapper__.columns.keys():
        value = getattr(row, key)
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
        elif key.endswith("_json"):
            payload[key] = decode_json(value) if isinstance(value, str) else value
        else:
            payload[key] = value
    return payload


def _model_performance(session: Session, *, category: str) -> dict[str, dict[str, Any]]:
    leaderboard = _latest_leaderboard_by_model(session)
    confidence = _latest_confidence_by_model(session, category=category)
    performance: dict[str, dict[str, Any]] = {}
    for model_name in META_CANDIDATE_MODELS:
        row = {**leaderboard.get(model_name, {}), **confidence.get(model_name, {})}
        if row:
            performance[model_name] = row
    return {
        "model_recent_performance": performance,
        "category_performance": confidence,
    }


def _latest_leaderboard_by_model(session: Session) -> dict[str, dict[str, Any]]:
    rows = list(
        session.scalars(
            select(ModelLeaderboard).order_by(
                ModelLeaderboard.model_name,
                desc(ModelLeaderboard.generated_at),
                desc(ModelLeaderboard.id),
            )
        )
    )
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.model_name in latest:
            continue
        latest[row.model_name] = {
            "forecast_count": row.forecast_count,
            "evaluated_forecast_count": row.evaluated_forecast_count,
            "settled_trade_count": row.settled_trade_count,
            "brier_score": row.brier_score,
            "log_loss": row.log_loss,
            "roi_on_exposure": row.roi_on_exposure,
            "win_rate": row.win_rate,
            "notes": row.notes,
        }
    return latest


def _latest_confidence_by_model(
    session: Session,
    *,
    category: str,
) -> dict[str, dict[str, Any]]:
    rows = list(
        session.scalars(
            select(ModelConfidenceScore)
            .where(ModelConfidenceScore.category.in_((category, "general")))
            .order_by(
                ModelConfidenceScore.model_name,
                desc(ModelConfidenceScore.generated_at),
                desc(ModelConfidenceScore.id),
            )
        )
    )
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        existing = latest.get(row.model_name)
        if existing is not None and existing.get("category") == category:
            continue
        if existing is not None and row.category == "general":
            continue
        latest[row.model_name] = {
            "category": row.category,
            "confidence_score": row.confidence_score,
            "confidence_label": row.confidence_label,
            "settled_trade_count": row.settled_trade_count,
            "brier_score": row.brier_score,
            "roi_on_exposure": row.roi_on_exposure,
            "status": row.status,
        }
    return latest


def _numeric_probabilities(model_probabilities: dict[str, Any]) -> list[Decimal]:
    values = [to_decimal(value) for value in model_probabilities.values()]
    return [value for value in values if value is not None]


def _market_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    if yes_bid is not None and yes_ask is not None:
        return midpoint(yes_bid, yes_ask)
    return to_decimal(snapshot.last_price_dollars)


def _distance_from_market(
    model_probabilities: dict[str, Any],
    snapshot: MarketSnapshot,
) -> dict[str, str]:
    market = _market_midpoint(snapshot)
    if market is None:
        return {}
    distances: dict[str, str] = {}
    for model_name, value in model_probabilities.items():
        probability = to_decimal(value)
        if probability is not None:
            distances[model_name] = decimal_to_str(probability - market) or "0"
    return distances


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
