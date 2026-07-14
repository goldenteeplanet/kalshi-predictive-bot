from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    BacktestTrade,
    CryptoFeature,
    CryptoMarketLink,
    FeatureSnapshot,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    MicrostructureEvent,
    MicrostructureFeature,
    ModelLeaderboard,
    ModelTournamentResult,
    NewsFeature,
    PaperFill,
    PaperPnl,
    PaperPosition,
    Settlement,
    WeatherFeature,
    WeatherMarketLink,
)
from kalshi_predictor.microstructure.repository import (
    latest_microstructure_feature,
    recent_microstructure_events,
)
from kalshi_predictor.opportunities.market_identity import (
    market_identity_fields,
    verify_market_identity,
)
from kalshi_predictor.opportunities.payout_scoring import payout_metrics_from_ranking
from kalshi_predictor.signals.attribution import ActiveSignal, extract_active_signals
from kalshi_predictor.ui.market_display import (
    classify_market_category,
    format_edge_cents,
    format_probability,
    recommendation_label,
    summarize_market_title,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


def build_opportunity_evidence(
    session: Session,
    *,
    ticker: str,
    model_name: str = "ensemble_v2",
) -> dict[str, Any]:
    ranking = latest_ranking(session, ticker=ticker, model_name=model_name)
    market = session.get(Market, ticker)
    forecast = latest_forecast(session, ticker=ticker, model_name=model_name)
    snapshot = latest_snapshot(session, ticker=ticker)
    feature_snapshot = latest_feature_snapshot(session, ticker=ticker)
    leaderboard = latest_leaderboard(session, model_name=model_name)
    tournament = latest_tournament_result(session, model_name=model_name)
    position = session.get(PaperPosition, ticker)
    pnl = latest_pnl(session, ticker=ticker)
    fills = recent_fills(session, ticker=ticker)
    backtests = recent_backtests(session, ticker=ticker, model_name=model_name)
    settlement = session.get(Settlement, ticker)

    identity = verify_market_identity(session, ticker=ticker, ranking=ranking, market=market)
    title = identity.market_title or _title(ticker=ticker, ranking=ranking, market=market)
    category = identity.category or classify_market_category(
        title,
        ranking.series_ticker if ranking else None,
    )
    crypto_link, crypto_feature = latest_crypto_context(session, ticker=ticker)
    weather_link, weather_feature = latest_weather_context(session, ticker=ticker)
    news_feature = latest_news_context(session, ticker=ticker)
    microstructure_feature = latest_microstructure_feature(session, ticker)
    microstructure_events = recent_microstructure_events(session, ticker=ticker, limit=5)
    metrics = payout_metrics_from_ranking(ranking) if ranking is not None else None
    rank = current_rank_for_ticker(session, ticker=ticker, model_name=model_name)
    active_signals = extract_active_signals(
        session,
        ticker=ticker,
        model_name=model_name,
        forecast=forecast,
        ranking=ranking,
        snapshot=snapshot,
    )

    component_models = _component_models(forecast)
    missing_data = _missing_data(
        ranking=ranking,
        forecast=forecast,
        snapshot=snapshot,
        feature_snapshot=feature_snapshot,
        category=category,
        crypto_feature=crypto_feature,
        weather_feature=weather_feature,
        leaderboard=leaderboard,
        backtests=backtests,
    )
    if identity.diagnostic_only:
        missing_data.append(f"Market identity: {identity.reason}")
    supporting_signals = _supporting_signals(
        ranking=ranking,
        forecast=forecast,
        snapshot=snapshot,
        leaderboard=leaderboard,
        metrics=metrics,
        crypto_link=crypto_link,
        crypto_feature=crypto_feature,
        weather_link=weather_link,
        weather_feature=weather_feature,
        news_feature=news_feature,
        microstructure_feature=microstructure_feature,
        microstructure_events=microstructure_events,
        active_signals=active_signals,
    )
    risk_factors = _risk_factors(
        ranking=ranking,
        snapshot=snapshot,
        backtests=backtests,
        forecast=forecast,
        microstructure_feature=microstructure_feature,
    )
    primary_signal = _primary_signal(
        active_signals=active_signals,
        ranking=ranking,
        metrics=metrics,
    )

    evidence = {
        "found": any(item is not None for item in (ranking, market, forecast, snapshot)),
        "ticker": ticker,
        "market_title": title,
        "short_market_name": summarize_market_title(title),
        "category": category,
        "rank": rank,
        "opportunity_score": ranking.opportunity_score if ranking else None,
        "side": ranking.best_side if ranking else None,
        "side_label": recommendation_label(ranking.best_side if ranking else None),
        "edge": ranking.estimated_edge if ranking else None,
        "edge_cents": format_edge_cents(ranking.estimated_edge if ranking else None),
        "market_price": _market_price(ranking=ranking, snapshot=snapshot),
        "model_probability": _model_probability(ranking=ranking, forecast=forecast),
        "model_probability_label": format_probability(
            _model_probability(ranking=ranking, forecast=forecast)
        ),
        "model_name": model_name,
        "component_models": component_models,
        "primary_signal": primary_signal,
        "signal_badges": [signal.as_badge() for signal in active_signals[:3]],
        "supporting_signals": supporting_signals,
        "risk_factors": risk_factors,
        "missing_data": missing_data,
        "data_freshness": _data_freshness(snapshot),
        "liquidity_status": _liquidity_status(ranking),
        "spread_status": _spread_status(ranking),
        "model_confidence": _model_confidence(ranking),
        "payout_if_correct": _payout_if_correct(ranking),
        "downside_if_wrong": _downside_if_wrong(ranking),
        "expected_value": decimal_to_str(metrics.expected_value if metrics else None),
        "payout_to_risk_ratio": decimal_to_str(metrics.payout_to_risk_ratio if metrics else None),
        "payout_adjusted_score": decimal_to_str(metrics.payout_adjusted_score if metrics else None),
        "paper_position": _paper_position(position),
        "paper_pnl": _paper_pnl(pnl),
        "historical_performance": _historical_performance(
            leaderboard=leaderboard,
            tournament=tournament,
            backtests=backtests,
            settlement=settlement,
        ),
        "recent_fills": [_fill_row(row) for row in fills],
        "backtest_history": [_backtest_row(row) for row in backtests],
        "market_snapshot": _snapshot_row(snapshot),
        "feature_snapshot": _feature_snapshot_row(feature_snapshot),
        "crypto_context": _crypto_context(crypto_link, crypto_feature),
        "weather_context": _weather_context(weather_link, weather_feature),
        "news_context": _news_context(news_feature),
        "microstructure_context": _microstructure_context(
            microstructure_feature,
            microstructure_events,
        ),
        "leaderboard": _leaderboard_row(leaderboard),
        "tournament": _tournament_row(tournament),
        "ranked_at": ranking.ranked_at.isoformat() if ranking else None,
        "forecasted_at": forecast.forecasted_at.isoformat() if forecast else None,
        "captured_at": snapshot.captured_at.isoformat() if snapshot else None,
    }
    evidence.update(market_identity_fields(identity))
    evidence["market_identity"] = identity.as_dict()
    return evidence


def top_opportunity_evidence(
    session: Session,
    *,
    model_name: str = "ensemble_v2",
    limit: int = 5,
) -> list[dict[str, Any]]:
    return [
        build_opportunity_evidence(session, ticker=row.ticker, model_name=model_name)
        for row in current_rankings(session, model_name=model_name, limit=limit)
    ]


def current_rankings(
    session: Session,
    *,
    model_name: str = "ensemble_v2",
    limit: int = 20,
) -> list[MarketRanking]:
    rows = list(
        session.scalars(
            select(MarketRanking)
            .where(MarketRanking.forecast_model == model_name)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
            .limit(max(limit * 20, 200))
        )
    )
    seen: set[str] = set()
    current: list[MarketRanking] = []
    for row in rows:
        if row.ticker in seen:
            continue
        seen.add(row.ticker)
        current.append(row)
    current.sort(key=lambda row: to_decimal(row.opportunity_score) or Decimal("0"), reverse=True)
    return current[:limit]


def current_rank_for_ticker(
    session: Session,
    *,
    ticker: str,
    model_name: str = "ensemble_v2",
) -> int | None:
    rankings = current_rankings(session, model_name=model_name, limit=100)
    for index, row in enumerate(rankings, start=1):
        if row.ticker == ticker:
            return index
    return None


def latest_ranking(
    session: Session,
    *,
    ticker: str,
    model_name: str | None = None,
) -> MarketRanking | None:
    statement = select(MarketRanking).where(MarketRanking.ticker == ticker)
    if model_name:
        statement = statement.where(MarketRanking.forecast_model == model_name)
    return session.scalar(statement.order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id)))


def latest_forecast(
    session: Session,
    *,
    ticker: str,
    model_name: str | None = None,
) -> Forecast | None:
    statement = select(Forecast).where(Forecast.ticker == ticker)
    if model_name:
        statement = statement.where(Forecast.model_name == model_name)
    return session.scalar(statement.order_by(desc(Forecast.forecasted_at), desc(Forecast.id)))


def latest_snapshot(session: Session, *, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
    )


def latest_feature_snapshot(session: Session, *, ticker: str) -> FeatureSnapshot | None:
    return session.scalar(
        select(FeatureSnapshot)
        .where(FeatureSnapshot.ticker == ticker)
        .order_by(desc(FeatureSnapshot.captured_at), desc(FeatureSnapshot.id))
    )


def latest_leaderboard(session: Session, *, model_name: str) -> ModelLeaderboard | None:
    return session.scalar(
        select(ModelLeaderboard)
        .where(ModelLeaderboard.model_name == model_name)
        .order_by(desc(ModelLeaderboard.generated_at), desc(ModelLeaderboard.id))
    )


def latest_tournament_result(
    session: Session,
    *,
    model_name: str,
) -> ModelTournamentResult | None:
    return session.scalar(
        select(ModelTournamentResult)
        .where(ModelTournamentResult.model_name == model_name)
        .order_by(desc(ModelTournamentResult.id))
    )


def latest_pnl(session: Session, *, ticker: str) -> PaperPnl | None:
    return session.scalar(
        select(PaperPnl)
        .where(PaperPnl.ticker == ticker)
        .order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id))
    )


def recent_fills(session: Session, *, ticker: str, limit: int = 5) -> list[PaperFill]:
    return list(
        session.scalars(
            select(PaperFill)
            .where(PaperFill.ticker == ticker)
            .order_by(desc(PaperFill.filled_at), desc(PaperFill.id))
            .limit(limit)
        )
    )


def recent_backtests(
    session: Session,
    *,
    ticker: str,
    model_name: str | None = None,
    limit: int = 5,
) -> list[BacktestTrade]:
    statement = select(BacktestTrade).where(BacktestTrade.ticker == ticker)
    if model_name:
        statement = statement.join(Forecast, BacktestTrade.forecast_id == Forecast.id).where(
            Forecast.model_name == model_name
        )
    statement = statement.order_by(
        desc(BacktestTrade.simulated_at),
        desc(BacktestTrade.id),
    )
    return list(session.scalars(statement))[:limit]


def latest_crypto_context(
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


def latest_weather_context(
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


def latest_news_context(
    session: Session,
    *,
    ticker: str,
) -> NewsFeature | None:
    return session.scalar(
        select(NewsFeature)
        .where(NewsFeature.ticker == ticker)
        .order_by(desc(NewsFeature.created_at), desc(NewsFeature.id))
    )


def _title(*, ticker: str, ranking: MarketRanking | None, market: Market | None) -> str:
    return str((market.title if market else None) or (ranking.title if ranking else None) or ticker)


def _market_price(
    *,
    ranking: MarketRanking | None,
    snapshot: MarketSnapshot | None,
) -> str | None:
    if ranking and ranking.best_price:
        return ranking.best_price
    if snapshot is None:
        return None
    return snapshot.best_yes_ask or snapshot.yes_ask_dollars or snapshot.last_price_dollars


def _model_probability(
    *,
    ranking: MarketRanking | None,
    forecast: Forecast | None,
) -> str | None:
    return (forecast.yes_probability if forecast else None) or (
        ranking.forecast_probability if ranking else None
    )


def _component_models(forecast: Forecast | None) -> dict[str, Any]:
    feature_json = decode_json(forecast.feature_json if forecast else None)
    components = feature_json.get("component_forecasts") or feature_json.get("components") or {}
    return components if isinstance(components, dict) else {}


def _supporting_signals(
    *,
    ranking: MarketRanking | None,
    forecast: Forecast | None,
    snapshot: MarketSnapshot | None,
    leaderboard: ModelLeaderboard | None,
    metrics: Any,
    crypto_link: CryptoMarketLink | None,
    crypto_feature: CryptoFeature | None,
    weather_link: WeatherMarketLink | None,
    weather_feature: WeatherFeature | None,
    news_feature: NewsFeature | None,
    microstructure_feature: MicrostructureFeature | None,
    microstructure_events: list[MicrostructureEvent],
    active_signals: list[ActiveSignal],
) -> list[str]:
    signals: list[str] = []
    for signal in active_signals[:3]:
        signals.append(
            f"{signal.signal_name}: {signal.description} "
            f"Confidence {decimal_to_str(signal.confidence) or '0'}."
        )
    if ranking is not None:
        signals.append(
            "Model probability "
            f"{format_probability(_model_probability(ranking=ranking, forecast=forecast))} "
            f"versus market price {ranking.best_price or 'n/a'}."
        )
        signals.append(f"Estimated edge is {format_edge_cents(ranking.estimated_edge)}.")
        signals.append(f"Opportunity score is {ranking.opportunity_score}.")
        signals.append(_liquidity_status(ranking))
        signals.append(_spread_status(ranking))
    if metrics is not None and metrics.payout_to_risk_ratio is not None:
        signals.append(f"Payout/risk ratio is {decimal_to_str(metrics.payout_to_risk_ratio)}.")
    if crypto_link is not None and crypto_feature is not None:
        movement = _percentage_label(crypto_feature.return_24h)
        signals.append(
            f"Crypto link: {crypto_link.symbol} trend is "
            f"{crypto_feature.trend_direction}; 24h return {movement}."
        )
    if weather_link is not None and weather_feature is not None:
        signals.append(
            "Weather link: "
            f"{weather_link.weather_metric} near {weather_link.location_key} has confidence "
            f"{weather_feature.weather_confidence_score or 'n/a'}."
        )
    if news_feature is not None:
        signals.append(
            "News link: "
            f"{news_feature.news_count} recent item(s), sentiment "
            f"{news_feature.avg_sentiment or 'n/a'}, max importance "
            f"{news_feature.max_importance or 'n/a'}."
        )
    if microstructure_feature is not None:
        signals.append(
            "Microstructure: spread change "
            f"{microstructure_feature.spread_change or 'n/a'}, liquidity change "
            f"{microstructure_feature.liquidity_change_pct or 'n/a'}, orderbook imbalance "
            f"{microstructure_feature.orderbook_imbalance or 'n/a'}."
        )
        flow_score = to_decimal(microstructure_feature.smart_money_score) or Decimal("0")
        if flow_score >= Decimal("0.70"):
            signals.append(
                "Microstructure caution: possible informed flow heuristic is active, "
                "but it is not proof of smart money."
            )
        for event in microstructure_events[:2]:
            signals.append(f"Microstructure event: {event.event_type} - {event.description}")
    if snapshot is not None:
        signals.append(_data_freshness(snapshot))
    if leaderboard is not None:
        signals.append(
            f"Leaderboard: {leaderboard.model_name} has ROI "
            f"{leaderboard.roi_on_exposure or 'n/a'} and Brier "
            f"{leaderboard.brier_score or 'n/a'}."
        )
    return signals or ["No supporting signals are available yet."]


def _risk_factors(
    *,
    ranking: MarketRanking | None,
    snapshot: MarketSnapshot | None,
    backtests: list[BacktestTrade],
    forecast: Forecast | None,
    microstructure_feature: MicrostructureFeature | None,
) -> list[str]:
    risks: list[str] = []
    if ranking is None:
        risks.append("No opportunity ranking is available for this ticker/model.")
    else:
        liquidity_score = to_decimal(ranking.liquidity_score) or Decimal("0")
        confidence = to_decimal(ranking.model_confidence_score) or Decimal("0")
        spread = to_decimal(ranking.spread)
        time_to_close = to_decimal(ranking.time_to_close_minutes)
        if liquidity_score < Decimal("60"):
            risks.append("Low liquidity may make the displayed edge hard to capture.")
        if spread is None:
            risks.append("Spread data is missing.")
        elif spread > Decimal("0.10"):
            risks.append("Wide spread may erase the edge before a fill.")
        if confidence < Decimal("60"):
            risks.append("Model confidence is still weak.")
        if time_to_close is not None and time_to_close < Decimal("60"):
            risks.append("Market expires soon, leaving little time to react.")
    if snapshot is None:
        risks.append("No fresh market snapshot is available.")
    elif _minutes_old(snapshot.captured_at) > Decimal("15"):
        risks.append("Market data is stale.")
    if forecast is None:
        risks.append("No latest forecast is available for this model.")
    if microstructure_feature is not None:
        micro_confidence = to_decimal(microstructure_feature.microstructure_confidence) or Decimal(
            "0"
        )
        flow_score = to_decimal(microstructure_feature.smart_money_score) or Decimal("0")
        if micro_confidence < Decimal("50"):
            risks.append(
                "Microstructure signal confidence is low; treat orderbook movement as a weak hint."
            )
        if flow_score >= Decimal("0.70"):
            risks.append("Possible informed flow is a heuristic, not proof.")
    if not backtests:
        risks.append("Not enough backtest history yet.")
    return risks or ["Standard paper/demo review risk only."]


def _primary_signal(
    *,
    active_signals: list[ActiveSignal],
    ranking: MarketRanking | None,
    metrics: Any,
) -> str:
    if active_signals:
        return active_signals[0].signal_name
    if ranking is None:
        return "No ranking exists for this ticker/model yet."
    edge = to_decimal(ranking.estimated_edge)
    if edge is not None and edge >= Decimal("0.05"):
        return "Market Divergence Signal"
    if metrics is not None and metrics.expected_value is not None and metrics.expected_value > 0:
        return "Opportunity Score Signal"
    return "Insufficient Edge"


def _missing_data(
    *,
    ranking: MarketRanking | None,
    forecast: Forecast | None,
    snapshot: MarketSnapshot | None,
    feature_snapshot: FeatureSnapshot | None,
    category: str,
    crypto_feature: CryptoFeature | None,
    weather_feature: WeatherFeature | None,
    leaderboard: ModelLeaderboard | None,
    backtests: list[BacktestTrade],
) -> list[str]:
    missing: list[str] = []
    if ranking is None:
        missing.append("opportunity ranking")
    if forecast is None:
        missing.append("latest forecast")
    if snapshot is None:
        missing.append("market snapshot")
    if feature_snapshot is None:
        missing.append("feature snapshot")
    if category == "Crypto" and crypto_feature is None:
        missing.append("crypto features")
    if category == "Weather" and weather_feature is None:
        missing.append("weather features")
    if leaderboard is None:
        missing.append("model leaderboard")
    if not backtests:
        missing.append("backtest history")
    return missing


def _data_freshness(snapshot: MarketSnapshot | None) -> str:
    if snapshot is None:
        return "No market snapshot is available."
    minutes = _minutes_old(snapshot.captured_at)
    return f"Latest market snapshot is {minutes:.0f} minutes old."


def _minutes_old(value: datetime) -> Decimal:
    observed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    seconds = max((utc_now() - observed).total_seconds(), 0)
    return Decimal(str(seconds)) / Decimal("60")


def _liquidity_status(ranking: MarketRanking | None) -> str:
    if ranking is None:
        return "Liquidity status is unknown."
    score = to_decimal(ranking.liquidity_score) or Decimal("0")
    if score >= Decimal("75"):
        return f"Liquidity looks healthy with score {ranking.liquidity_score}."
    if score >= Decimal("60"):
        return f"Liquidity is acceptable but should be watched: score {ranking.liquidity_score}."
    return f"Liquidity is weak with score {ranking.liquidity_score}."


def _spread_status(ranking: MarketRanking | None) -> str:
    if ranking is None:
        return "Spread status is unknown."
    spread = to_decimal(ranking.spread)
    if spread is None:
        return "Spread is missing."
    if spread <= Decimal("0.05"):
        return f"Spread is tight at {ranking.spread}."
    if spread <= Decimal("0.10"):
        return f"Spread is workable at {ranking.spread}."
    return f"Spread is wide at {ranking.spread}."


def _model_confidence(ranking: MarketRanking | None) -> str:
    if ranking is None:
        return "Model confidence is unknown."
    score = to_decimal(ranking.model_confidence_score) or Decimal("0")
    if score >= Decimal("75"):
        return f"Strong model confidence: {ranking.model_confidence_score}."
    if score >= Decimal("60"):
        return f"Moderate model confidence: {ranking.model_confidence_score}."
    return f"Weak model confidence: {ranking.model_confidence_score}."


def _payout_if_correct(ranking: MarketRanking | None) -> str | None:
    if ranking is None:
        return None
    price = to_decimal(ranking.best_price)
    if price is None:
        return None
    return decimal_to_str(Decimal("1") - price)


def _downside_if_wrong(ranking: MarketRanking | None) -> str | None:
    return ranking.best_price if ranking else None


def _paper_position(position: PaperPosition | None) -> str:
    if position is None:
        return "No open paper position."
    return (
        f"YES {position.yes_contracts}, NO {position.no_contracts}, "
        f"realized P&L {position.realized_pnl}."
    )


def _paper_pnl(pnl: PaperPnl | None) -> dict[str, Any]:
    if pnl is None:
        return {"available": False, "summary": "No paper P&L row yet."}
    return {
        "available": True,
        "calculated_at": pnl.calculated_at.isoformat(),
        "realized_pnl": pnl.realized_pnl,
        "unrealized_pnl": pnl.unrealized_pnl,
        "total_pnl": pnl.total_pnl,
    }


def _historical_performance(
    *,
    leaderboard: ModelLeaderboard | None,
    tournament: ModelTournamentResult | None,
    backtests: list[BacktestTrade],
    settlement: Settlement | None,
) -> dict[str, Any]:
    return {
        "leaderboard_roi": leaderboard.roi_on_exposure if leaderboard else None,
        "leaderboard_brier": leaderboard.brier_score if leaderboard else None,
        "leaderboard_win_rate": leaderboard.win_rate if leaderboard else None,
        "tournament_rank": tournament.overall_rank if tournament else None,
        "recent_backtests": len(backtests),
        "settlement_result": settlement.result if settlement else None,
        "summary": _historical_summary(leaderboard, tournament, backtests),
    }


def _historical_summary(
    leaderboard: ModelLeaderboard | None,
    tournament: ModelTournamentResult | None,
    backtests: list[BacktestTrade],
) -> str:
    if leaderboard is None and tournament is None and not backtests:
        return "Not enough historical performance data yet."
    parts: list[str] = []
    if leaderboard is not None:
        parts.append(
            f"leaderboard ROI {leaderboard.roi_on_exposure or 'n/a'}, "
            f"Brier {leaderboard.brier_score or 'n/a'}"
        )
    if tournament is not None:
        parts.append(f"tournament overall rank {tournament.overall_rank or 'n/a'}")
    if backtests:
        parts.append(f"{len(backtests)} recent backtest trades")
    return "; ".join(parts)


def _fill_row(row: PaperFill) -> dict[str, Any]:
    return {
        "filled_at": row.filled_at.isoformat(),
        "side": row.side,
        "price": row.price,
        "quantity": row.quantity,
    }


def _backtest_row(row: BacktestTrade) -> dict[str, Any]:
    return {
        "simulated_at": row.simulated_at.isoformat(),
        "side": row.side,
        "price": row.price,
        "edge": row.edge,
        "pnl": row.pnl,
        "settlement_result": row.settlement_result,
    }


def _snapshot_row(snapshot: MarketSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    return {
        "captured_at": snapshot.captured_at.isoformat(),
        "status": snapshot.status,
        "best_yes_bid": snapshot.best_yes_bid,
        "best_yes_ask": snapshot.best_yes_ask,
        "spread": snapshot.spread,
        "volume": snapshot.volume_fp,
        "open_interest": snapshot.open_interest_fp,
    }


def _feature_snapshot_row(snapshot: FeatureSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    return {
        "captured_at": snapshot.captured_at.isoformat(),
        "market_features": decode_json(snapshot.market_features_json),
        "external_features": decode_json(snapshot.external_features_json),
        "combined_features": decode_json(snapshot.combined_features_json),
    }


def _crypto_context(
    link: CryptoMarketLink | None,
    feature: CryptoFeature | None,
) -> dict[str, Any]:
    if link is None:
        return {"available": False}
    return {
        "available": feature is not None,
        "symbol": link.symbol,
        "confidence": link.confidence,
        "reason": link.reason,
        "trend_direction": feature.trend_direction if feature else None,
        "return_24h": feature.return_24h if feature else None,
        "momentum_score": feature.momentum_score if feature else None,
    }


def _weather_context(
    link: WeatherMarketLink | None,
    feature: WeatherFeature | None,
) -> dict[str, Any]:
    if link is None:
        return {"available": False}
    return {
        "available": feature is not None,
        "location_key": link.location_key,
        "weather_metric": link.weather_metric,
        "target_operator": link.target_operator,
        "target_value": link.target_value,
        "confidence": link.confidence,
        "temperature_f": feature.temperature_f if feature else None,
        "precipitation_probability": feature.precipitation_probability if feature else None,
        "weather_confidence_score": feature.weather_confidence_score if feature else None,
    }


def _news_context(feature: NewsFeature | None) -> dict[str, Any]:
    if feature is None:
        return {"available": False}
    return {
        "available": True,
        "created_at": feature.created_at.isoformat(),
        "window_minutes": feature.feature_window_minutes,
        "news_count": feature.news_count,
        "high_importance_count": feature.high_importance_count,
        "avg_sentiment": feature.avg_sentiment,
        "max_importance": feature.max_importance,
        "freshness_score": feature.freshness_score,
        "category_counts": decode_json(feature.category_counts_json),
        "entity_counts": decode_json(feature.entity_counts_json),
        "linked_news": decode_json(feature.linked_news_json),
    }


def _microstructure_context(
    feature: MicrostructureFeature | None,
    events: list[MicrostructureEvent],
) -> dict[str, Any]:
    if feature is None:
        return {"available": False}
    return {
        "available": True,
        "created_at": feature.created_at.isoformat(),
        "lookback_minutes": feature.lookback_minutes,
        "snapshot_count": feature.snapshot_count,
        "spread_change": feature.spread_change,
        "liquidity_change_pct": feature.liquidity_change_pct,
        "orderbook_imbalance": feature.orderbook_imbalance,
        "late_move_score": feature.late_move_score,
        "dislocation_score": feature.dislocation_score,
        "smart_money_score": feature.smart_money_score,
        "microstructure_confidence": feature.microstructure_confidence,
        "events": [_microstructure_event_row(row) for row in events],
        "warning": "Possible informed flow is a heuristic, not proof.",
    }


def _microstructure_event_row(row: MicrostructureEvent) -> dict[str, Any]:
    return {
        "created_at": row.created_at.isoformat(),
        "event_type": row.event_type,
        "severity": row.severity,
        "score": row.score,
        "description": row.description,
    }


def _leaderboard_row(row: ModelLeaderboard | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "generated_at": row.generated_at.isoformat(),
        "forecast_count": row.forecast_count,
        "paper_trade_count": row.paper_trade_count,
        "brier_score": row.brier_score,
        "win_rate": row.win_rate,
        "roi_on_exposure": row.roi_on_exposure,
        "notes": row.notes,
    }


def _tournament_row(row: ModelTournamentResult | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "category": row.category,
        "overall_rank": row.overall_rank,
        "status": row.status,
        "brier_score": row.brier_score,
        "roi_on_exposure": row.roi_on_exposure,
        "notes": row.notes,
    }


def _percentage_label(value: Any) -> str:
    decimal = to_decimal(value)
    if decimal is None:
        return "n/a"
    return f"{decimal * Decimal('100'):.1f}%"
