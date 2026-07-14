from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, aliased

from kalshi_predictor.active_universe import is_inactive_market_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Forecast, Market, MarketSnapshot
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.learning.repository import insert_learning_rejection
from kalshi_predictor.opportunities.payout_scoring import calculate_payout_metrics
from kalshi_predictor.opportunities.repository import (
    insert_market_opportunity,
    insert_market_ranking,
)
from kalshi_predictor.opportunities.scoring import (
    score_liquidity,
    score_model_confidence,
    score_spread,
    score_time_to_close,
)
from kalshi_predictor.paper.ledger import (
    get_latest_forecast_per_ticker,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.utils.decimals import ONE_DOLLAR, decimal_to_str, midpoint, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now


@dataclass(frozen=True)
class OpportunityScanSummary:
    markets_scanned: int
    rankings_inserted: int
    opportunities_detected: int
    top_opportunity_ticker: str | None
    top_opportunity_score: Decimal | None
    rankings: list[dict[str, Any]]
    opportunities: list[dict[str, Any]]
    scan_mode: str = "HISTORICAL_RESEARCH_SCAN"
    current_ticker_scope_count: int | None = None
    historical_rows_excluded: int = 0
    first_hard_blocker: str | None = None


def scan_opportunities(
    session: Session,
    *,
    model_name: str = "market_implied_v1",
    limit: int | None = None,
    settings: Settings | None = None,
    min_edge: Decimal | None = None,
    min_score: Decimal | None = None,
    ticker_scope: set[str] | list[str] | tuple[str, ...] | None = None,
    scan_mode: str = "HISTORICAL_RESEARCH_SCAN",
) -> OpportunityScanSummary:
    base_settings = settings or get_settings()
    resolved_settings = learning_paper_settings(base_settings)
    resolved_limit = limit or resolved_settings.opportunity_max_results
    edge_threshold = min_edge if min_edge is not None else resolved_settings.opportunity_min_edge
    score_threshold = (
        min_score if min_score is not None else resolved_settings.opportunity_min_score
    )
    ranked_at = utc_now()
    allowed_tickers = (
        {str(ticker) for ticker in ticker_scope if str(ticker).strip()}
        if ticker_scope is not None
        else None
    )
    all_forecasts = get_latest_forecast_per_ticker(session, model_name=model_name)
    forecasts = (
        [forecast for forecast in all_forecasts if forecast.ticker in allowed_tickers]
        if allowed_tickers is not None
        else all_forecasts
    )
    historical_rows_excluded = (
        max(0, len(all_forecasts) - len(forecasts)) if allowed_tickers is not None else 0
    )
    forecast_tickers = [forecast.ticker for forecast in forecasts]
    snapshots = _latest_snapshots_by_ticker(session, forecast_tickers)
    statuses = _market_status_by_ticker(
        session,
        forecast_tickers,
        snapshots=snapshots,
    )
    rankings: list[dict[str, Any]] = []
    opportunities: list[dict[str, Any]] = []

    for forecast in forecasts:
        snapshot = snapshots.get(forecast.ticker)
        if snapshot is None:
            continue
        if is_inactive_market_status(statuses.get(forecast.ticker)):
            continue
        ranking = build_market_ranking(
            forecast=forecast,
            snapshot=snapshot,
            settings=resolved_settings,
            ranked_at=ranked_at,
        )
        rankings.append(ranking)

    rankings.sort(
        key=lambda row: to_decimal(row["opportunity_score"]) or Decimal("0"),
        reverse=True,
    )
    selected_rankings = rankings[:resolved_limit]
    for ranking in selected_rankings:
        insert_market_ranking(session, ranking)
        edge = to_decimal(ranking.get("estimated_edge")) or Decimal("0")
        score = to_decimal(ranking.get("opportunity_score")) or Decimal("0")
        spread = to_decimal(ranking.get("spread"))
        liquidity = to_decimal(ranking.get("liquidity")) or Decimal("0")
        time_to_close = to_decimal(ranking.get("time_to_close_minutes"))
        qualifies = (
            edge >= edge_threshold
            and score >= score_threshold
            and (spread is None or spread <= resolved_settings.opportunity_max_spread)
            and liquidity >= resolved_settings.opportunity_min_liquidity
            and (
                time_to_close is None
                or time_to_close >= resolved_settings.opportunity_min_time_to_close_minutes
            )
            and ranking.get("best_side") is not None
            and ranking.get("best_price") is not None
        )
        if qualifies:
            opportunity = {
                "ticker": ranking["ticker"],
                "detected_at": ranked_at,
                "model_name": ranking["forecast_model"],
                "side": ranking["best_side"],
                "price": ranking["best_price"],
                "forecast_probability": ranking["forecast_probability"],
                "estimated_edge": ranking["estimated_edge"],
                "opportunity_score": ranking["opportunity_score"],
                "status": "OPEN",
                "reason": ranking["reason"],
                "raw_json": ranking,
            }
            insert_market_opportunity(session, opportunity)
            opportunities.append(opportunity)
        elif base_settings.learning_mode:
            reason = _rejection_reason(
                ranking=ranking,
                settings=resolved_settings,
                edge=edge,
                score=score,
                spread=spread,
                liquidity=liquidity,
                time_to_close=time_to_close,
            )
            _log_learning_rejection(
                session,
                ranking=ranking,
                settings=resolved_settings,
                edge=edge,
                score=score,
                spread=spread,
                liquidity=liquidity,
                time_to_close=time_to_close,
                reason=reason,
            )

    top = (
        opportunities[0]
        if opportunities
        else (selected_rankings[0] if selected_rankings else None)
    )
    return OpportunityScanSummary(
        markets_scanned=len(forecasts),
        rankings_inserted=len(selected_rankings),
        opportunities_detected=len(opportunities),
        top_opportunity_ticker=str(top["ticker"]) if top else None,
        top_opportunity_score=to_decimal(top["opportunity_score"]) if top else None,
        rankings=selected_rankings,
        opportunities=opportunities,
        scan_mode=scan_mode,
        current_ticker_scope_count=len(allowed_tickers) if allowed_tickers is not None else None,
        historical_rows_excluded=historical_rows_excluded,
        first_hard_blocker=_first_hard_blocker(selected_rankings, opportunities, resolved_settings),
    )


def _latest_snapshots_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, MarketSnapshot]:
    unique_tickers = sorted({ticker for ticker in tickers if ticker})
    if not unique_tickers:
        return {}
    statement = (
        select(
            MarketSnapshot,
            func.row_number()
            .over(
                partition_by=MarketSnapshot.ticker,
                order_by=(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id)),
            )
            .label("row_number"),
        )
        .where(MarketSnapshot.ticker.in_(unique_tickers))
        .subquery()
    )
    snapshot = aliased(MarketSnapshot, statement)
    return {
        row.ticker: row
        for row in session.scalars(select(snapshot).where(statement.c.row_number == 1))
    }


def _market_status_by_ticker(
    session: Session,
    tickers: list[str],
    *,
    snapshots: dict[str, MarketSnapshot],
) -> dict[str, str | None]:
    unique_tickers = sorted({ticker for ticker in tickers if ticker})
    if not unique_tickers:
        return {}
    statuses = {
        ticker: status
        for ticker, status in session.execute(
            select(Market.ticker, Market.status).where(Market.ticker.in_(unique_tickers))
        )
    }
    for ticker in unique_tickers:
        if statuses.get(ticker):
            continue
        snapshot = snapshots.get(ticker)
        statuses[ticker] = snapshot.status if snapshot is not None else None
    return statuses


def build_market_ranking(
    *,
    forecast: Forecast,
    snapshot: MarketSnapshot,
    settings: Settings,
    ranked_at: Any,
) -> dict[str, Any]:
    raw_market = decode_json(snapshot.raw_market_json)
    forecast_probability = to_decimal(forecast.yes_probability)
    best_yes_ask = to_decimal(snapshot.best_yes_ask) or to_decimal(forecast.best_yes_ask)
    best_no_ask = to_decimal(snapshot.best_no_ask)
    side, price, edge = _best_side(forecast_probability, best_yes_ask, best_no_ask)
    spread = to_decimal(snapshot.spread)
    if spread is None:
        yes_bid = to_decimal(snapshot.best_yes_bid)
        yes_ask = to_decimal(snapshot.best_yes_ask)
        spread = yes_ask - yes_bid if yes_bid is not None and yes_ask is not None else None
    midpoint_value = _midpoint(snapshot)
    time_to_close = _time_to_close_minutes(snapshot, raw_market)
    liquidity = raw_market.get("liquidity_dollars")
    liquidity_score = score_liquidity(
        volume=snapshot.volume_fp,
        open_interest=snapshot.open_interest_fp,
        liquidity=liquidity,
    )
    spread_score = score_spread(spread, max_spread=settings.opportunity_max_spread)
    time_score = score_time_to_close(
        time_to_close,
        min_minutes=settings.opportunity_min_time_to_close_minutes,
    )
    model_score = score_model_confidence(forecast_probability)
    payout_metrics = calculate_payout_metrics(
        side=side,
        yes_probability=forecast_probability,
        cost=price,
        edge=edge,
        liquidity_score=liquidity_score,
        spread_score=spread_score,
        confidence_score=model_score,
        time_score=time_score,
    )
    payout_fields = payout_metrics.as_dict()
    opportunity_score = payout_metrics.payout_adjusted_score
    reason = _reason(side, edge, opportunity_score, spread, time_to_close)
    return {
        "ticker": snapshot.ticker,
        "ranked_at": ranked_at,
        "title": raw_market.get("title"),
        "status": snapshot.status,
        "series_ticker": raw_market.get("series_ticker"),
        "event_ticker": raw_market.get("event_ticker"),
        "volume": snapshot.volume_fp,
        "open_interest": snapshot.open_interest_fp,
        "liquidity": liquidity,
        "spread": decimal_to_str(spread),
        "midpoint": decimal_to_str(midpoint_value),
        "time_to_close_minutes": decimal_to_str(time_to_close),
        "forecast_model": forecast.model_name,
        "forecast_probability": decimal_to_str(forecast_probability),
        "best_side": side,
        "best_price": decimal_to_str(price),
        "estimated_edge": decimal_to_str(edge),
        "liquidity_score": decimal_to_str(liquidity_score),
        "spread_score": decimal_to_str(spread_score),
        "time_score": decimal_to_str(time_score),
        "model_confidence_score": decimal_to_str(model_score),
        "opportunity_score": decimal_to_str(opportunity_score),
        **payout_fields,
        "reason": reason,
        "raw_json": {
            "forecast_id": forecast.id,
            "snapshot_id": snapshot.id,
            "best_yes_ask": decimal_to_str(best_yes_ask),
            "best_no_ask": decimal_to_str(best_no_ask),
            **payout_fields,
        },
    }


def _best_side(
    probability: Decimal | None,
    best_yes_ask: Decimal | None,
    best_no_ask: Decimal | None,
) -> tuple[str | None, Decimal | None, Decimal | None]:
    if probability is None:
        return None, None, None
    candidates: list[tuple[str, Decimal, Decimal]] = []
    if best_yes_ask is not None:
        candidates.append((BUY_YES, best_yes_ask, probability - best_yes_ask))
    if best_no_ask is not None:
        candidates.append((BUY_NO, best_no_ask, (ONE_DOLLAR - probability) - best_no_ask))
    if not candidates:
        return None, None, None
    side, price, edge = max(candidates, key=lambda candidate: candidate[2])
    return side, price, edge


def _midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    if bid is not None and ask is not None:
        return midpoint(bid, ask)
    return None


def _time_to_close_minutes(snapshot: MarketSnapshot, raw_market: dict[str, Any]) -> Decimal | None:
    close_time = parse_datetime(raw_market.get("close_time"))
    if close_time is None:
        return None
    captured_at = snapshot.captured_at
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    return Decimal(str((close_time - captured_at).total_seconds() / 60))


def _rejection_reason(
    *,
    ranking: dict[str, Any],
    settings: Settings,
    edge: Decimal,
    score: Decimal,
    spread: Decimal | None,
    liquidity: Decimal,
    time_to_close: Decimal | None,
) -> str:
    if ranking.get("best_side") is None or ranking.get("best_price") is None:
        return "missing_price"
    if edge < settings.opportunity_min_edge:
        return "low_edge"
    if score < settings.opportunity_min_score:
        return "low_score"
    if spread is not None and spread > settings.opportunity_max_spread:
        return "wide_spread"
    if liquidity < settings.opportunity_min_liquidity:
        return "low_liquidity"
    if (
        time_to_close is not None
        and time_to_close < settings.opportunity_min_time_to_close_minutes
    ):
        return "stale_data"
    return "confidence_too_low"


def _first_hard_blocker(
    rankings: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    settings: Settings,
) -> str:
    if opportunities:
        return "CURRENT_OPPORTUNITY_FOUND"
    if not rankings:
        return "RANKING_NOT_GENERATED_FOR_CURRENT_FORECAST"
    ranking = rankings[0]
    edge = to_decimal(ranking.get("estimated_edge")) or Decimal("0")
    executable_ev = edge - (to_decimal(ranking.get("spread")) or Decimal("0"))
    score = to_decimal(ranking.get("opportunity_score")) or Decimal("0")
    spread = to_decimal(ranking.get("spread"))
    liquidity = to_decimal(ranking.get("liquidity")) or Decimal("0")
    time_to_close = to_decimal(ranking.get("time_to_close_minutes"))
    if edge <= 0:
        return "EV_NOT_POSITIVE"
    if executable_ev <= 0:
        return "EXECUTABLE_EV_NOT_POSITIVE"
    if edge < settings.opportunity_min_edge:
        return "RANKING_FILTERED_BY_EV"
    if score < settings.opportunity_min_score:
        return "RANKING_FILTERED_BY_SCORE"
    if liquidity < settings.opportunity_min_liquidity:
        return "RANKING_FILTERED_BY_LIQUIDITY"
    if spread is not None and spread > settings.opportunity_max_spread:
        return "RANKING_FILTERED_BY_SPREAD"
    if (
        time_to_close is not None
        and time_to_close < settings.opportunity_min_time_to_close_minutes
    ):
        return "RANKING_FILTERED_BY_TIME_TO_CLOSE"
    return "UNKNOWN_REQUIRES_INVESTIGATION"


def _log_learning_rejection(
    session: Session,
    *,
    ranking: dict[str, Any],
    settings: Settings,
    edge: Decimal,
    score: Decimal,
    spread: Decimal | None,
    liquidity: Decimal,
    time_to_close: Decimal | None,
    reason: str,
) -> None:
    settlement_eta_hours = (
        time_to_close / Decimal("60") if time_to_close is not None else None
    )
    insert_learning_rejection(
        session,
        {
            "ticker": ranking["ticker"],
            "model_name": ranking["forecast_model"],
            "rejected_at": ranking["ranked_at"],
            "reason": reason,
            "edge": edge,
            "opportunity_score": score,
            "spread": spread,
            "liquidity": liquidity,
            "settlement_eta_hours": settlement_eta_hours,
            "raw_json": {
                "source": "opportunity_scanner",
                "thresholds": {
                    "min_edge": str(settings.opportunity_min_edge),
                    "min_score": str(settings.opportunity_min_score),
                    "max_spread": str(settings.opportunity_max_spread),
                    "min_liquidity": str(settings.opportunity_min_liquidity),
                },
                "ranking": ranking,
            },
        },
    )


def _reason(
    side: str | None,
    edge: Decimal | None,
    opportunity_score: Decimal,
    spread: Decimal | None,
    time_to_close: Decimal | None,
) -> str:
    if side is None or edge is None:
        return "No executable YES/NO ask price was available."
    notes = [f"{side} has the best estimated edge at {edge}."]
    notes.append(f"Opportunity score is {opportunity_score}.")
    if spread is None:
        notes.append("Spread missing; scanner scored spread conservatively.")
    if time_to_close is None:
        notes.append("Close time missing; scanner scored timing conservatively.")
    return " ".join(notes)
