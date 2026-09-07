from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import MarketOpportunity, MarketRanking
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def insert_market_ranking(session: Session, ranking: Mapping[str, Any]) -> MarketRanking:
    record = MarketRanking(
        ticker=str(ranking["ticker"]),
        ranked_at=ranking.get("ranked_at") or utc_now(),
        title=_optional_str(ranking.get("title")),
        status=_optional_str(ranking.get("status")),
        series_ticker=_optional_str(ranking.get("series_ticker")),
        event_ticker=_optional_str(ranking.get("event_ticker")),
        volume=_decimal_string(ranking.get("volume")),
        open_interest=_decimal_string(ranking.get("open_interest")),
        liquidity=_decimal_string(ranking.get("liquidity")),
        spread=_decimal_string(ranking.get("spread")),
        midpoint=_decimal_string(ranking.get("midpoint")),
        time_to_close_minutes=_decimal_string(ranking.get("time_to_close_minutes")),
        forecast_model=str(ranking["forecast_model"]),
        forecast_probability=_decimal_string(ranking.get("forecast_probability")),
        best_side=_optional_str(ranking.get("best_side")),
        best_price=_decimal_string(ranking.get("best_price")),
        estimated_edge=_decimal_string(ranking.get("estimated_edge")),
        liquidity_score=_decimal_string(ranking.get("liquidity_score")) or "0",
        spread_score=_decimal_string(ranking.get("spread_score")) or "0",
        time_score=_decimal_string(ranking.get("time_score")) or "0",
        model_confidence_score=_decimal_string(ranking.get("model_confidence_score")) or "0",
        opportunity_score=_decimal_string(ranking.get("opportunity_score")) or "0",
        reason=str(ranking.get("reason") or ""),
        raw_json=encode_json(dict(ranking.get("raw_json") or ranking)),
    )
    session.add(record)
    session.flush()
    from kalshi_predictor.memory.capture import capture_market_ranking

    capture_market_ranking(session, record)
    return record


def insert_market_opportunity(
    session: Session,
    opportunity: Mapping[str, Any],
) -> MarketOpportunity:
    record = MarketOpportunity(
        ticker=str(opportunity["ticker"]),
        detected_at=opportunity.get("detected_at") or utc_now(),
        model_name=str(opportunity["model_name"]),
        side=str(opportunity["side"]),
        price=_decimal_string(opportunity.get("price")) or "0",
        forecast_probability=_decimal_string(opportunity.get("forecast_probability")) or "0",
        estimated_edge=_decimal_string(opportunity.get("estimated_edge")) or "0",
        opportunity_score=_decimal_string(opportunity.get("opportunity_score")) or "0",
        status=str(opportunity.get("status") or "OPEN"),
        reason=str(opportunity.get("reason") or ""),
        raw_json=encode_json(dict(opportunity.get("raw_json") or opportunity)),
    )
    session.add(record)
    session.flush()
    from kalshi_predictor.memory.capture import capture_market_opportunity

    capture_market_opportunity(session, record)
    return record


def get_recent_rankings(session: Session, *, limit: int = 50) -> list[MarketRanking]:
    return list(
        session.scalars(
            select(MarketRanking)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.opportunity_score))
            .limit(limit)
        )
    )


def get_recent_opportunities(session: Session, *, limit: int = 20) -> list[MarketOpportunity]:
    return list(
        session.scalars(
            select(MarketOpportunity)
            .order_by(
                desc(MarketOpportunity.detected_at),
                desc(MarketOpportunity.opportunity_score),
            )
            .limit(limit)
        )
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _decimal_string(value: Any) -> str | None:
    if isinstance(value, Decimal):
        return decimal_to_str(value)
    return decimal_to_str(value)
