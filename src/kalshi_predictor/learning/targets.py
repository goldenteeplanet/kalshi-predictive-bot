from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_ticker_eligible_for_new_forecasts
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import LearningTradeTarget, MarketRanking
from kalshi_predictor.learning.config import learning_categories
from kalshi_predictor.learning.repository import (
    insert_learning_rejection,
    insert_learning_trade_target,
)
from kalshi_predictor.phase3ak import (
    multi_leg_learning_eligibility,
    phase3ak_learning_rejection_reason,
)
from kalshi_predictor.tournament.ranking import classify_market_category
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

CATEGORY_PRIORITY = {
    "crypto": Decimal("105"),
    "weather": Decimal("100"),
    "economic": Decimal("90"),
    "general": Decimal("65"),
    "sports": Decimal("20"),
    "unknown": Decimal("5"),
}


@dataclass(frozen=True)
class LearningTargetsResult:
    targets: list[dict[str, Any]]
    inserted: int
    scanned: int


def settlement_speed_score(minutes_to_close: Decimal | None) -> Decimal:
    if minutes_to_close is None:
        return Decimal("15")
    if minutes_to_close <= 0:
        return Decimal("100")
    days = minutes_to_close / Decimal("1440")
    if days <= Decimal("0.25"):
        return Decimal("100")
    if days <= Decimal("1"):
        return Decimal("90")
    if days <= Decimal("3"):
        return Decimal("70")
    if days <= Decimal("7"):
        return Decimal("35")
    return Decimal("10")


def learning_priority_score(
    *,
    edge: Decimal | None,
    opportunity_score: Decimal | None,
    confidence_score: Decimal | None,
    liquidity_score: Decimal | None = None,
    speed_score: Decimal,
    category_score: Decimal = Decimal("50"),
) -> Decimal:
    edge_score = min(Decimal("100"), max(Decimal("0"), (edge or Decimal("0")) * Decimal("1000")))
    quality = max(Decimal("0"), min(Decimal("100"), opportunity_score or Decimal("0")))
    confidence = max(Decimal("0"), min(Decimal("100"), confidence_score or Decimal("50")))
    liquidity = max(Decimal("0"), min(Decimal("100"), liquidity_score or Decimal("0")))
    category = max(Decimal("0"), min(Decimal("100"), category_score))
    score = (
        speed_score * Decimal("0.30")
        + edge_score * Decimal("0.25")
        + quality * Decimal("0.20")
        + confidence * Decimal("0.15")
        + liquidity * Decimal("0.05")
        + category * Decimal("0.05")
    )
    return score.quantize(Decimal("0.0001"))


def generate_learning_targets(
    session: Session,
    *,
    settings: Settings | None = None,
    model_name: str = "ensemble_v2",
    limit: int = 100,
    persist: bool = True,
) -> LearningTargetsResult:
    resolved_settings = settings or get_settings()
    categories = learning_categories(resolved_settings)
    generated_at = utc_now()
    rows = list(
        session.scalars(
            select(MarketRanking)
            .where(MarketRanking.forecast_model == model_name)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
            .limit(max(limit * 4, limit))
        )
    )
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ranking in rows:
        if ranking.ticker in seen:
            continue
        seen.add(ranking.ticker)
        spread = to_decimal(ranking.spread)
        liquidity = to_decimal(ranking.liquidity) or Decimal("0")
        minutes = to_decimal(ranking.time_to_close_minutes)
        category = classify_market_category(
            " ".join(
                part
                for part in (ranking.title, ranking.series_ticker, ranking.event_ticker)
                if part
            )
        )
        text = " ".join(
            part for part in (ranking.title, ranking.series_ticker, ranking.event_ticker) if part
        )
        if categories and category not in categories:
            continue
        if not is_ticker_eligible_for_new_forecasts(session, ranking.ticker):
            _log_target_rejection(
                session,
                ranking=ranking,
                settings=resolved_settings,
                category=category,
                spread=spread,
                liquidity=liquidity,
                minutes=minutes,
                reason_override="inactive_market",
                raw_extra={
                    "phase3as_gate": {
                        "status": "INELIGIBLE",
                        "reason": "closed_or_inactive_market",
                    }
                },
            )
            continue
        phase3ak_gate = None
        if category == "sports":
            phase3ak_gate = multi_leg_learning_eligibility(session, ranking.ticker)
            if (
                phase3ak_gate["status"] != "NOT_MULTILEG"
                and not phase3ak_gate["eligible"]
            ):
                _log_target_rejection(
                    session,
                    ranking=ranking,
                    settings=resolved_settings,
                    category=category,
                    spread=spread,
                    liquidity=liquidity,
                    minutes=minutes,
                    reason_override=phase3ak_learning_rejection_reason(phase3ak_gate),
                    raw_extra={"phase3ak_gate": phase3ak_gate},
                )
                continue
        if (
            spread is not None
            and spread > resolved_settings.learning_max_spread
            or liquidity < resolved_settings.learning_min_liquidity
            or _too_long(minutes, resolved_settings)
        ):
            _log_target_rejection(
                session,
                ranking=ranking,
                settings=resolved_settings,
                category=category,
                spread=spread,
                liquidity=liquidity,
                minutes=minutes,
            )
            continue
        speed = settlement_speed_score(minutes)
        category_score = category_priority_score(
            category=category,
            market_text=text,
            minutes_to_close=minutes,
        )
        priority = learning_priority_score(
            edge=to_decimal(ranking.estimated_edge),
            opportunity_score=to_decimal(ranking.opportunity_score),
            confidence_score=to_decimal(ranking.model_confidence_score),
            liquidity_score=to_decimal(ranking.liquidity_score),
            speed_score=speed,
            category_score=category_score,
        )
        reason = _target_reason(ranking, category, speed, priority)
        target = {
            "generated_at": generated_at,
            "ticker": ranking.ticker,
            "model_name": ranking.forecast_model,
            "category": category,
            "settlement_speed_score": speed,
            "learning_priority_score": priority,
            "reason": reason,
            "raw_json": {
                "ranking_id": ranking.id,
                "title": ranking.title,
                "edge": ranking.estimated_edge,
                "opportunity_score": ranking.opportunity_score,
                "spread": ranking.spread,
                "liquidity": ranking.liquidity,
                "liquidity_score": ranking.liquidity_score,
                "time_to_close_minutes": ranking.time_to_close_minutes,
                "category_priority_score": category_score,
                "deprioritized": _is_deprioritized_market(text),
                "phase3ak_gate": phase3ak_gate,
            },
        }
        targets.append(target)

    targets.sort(
        key=lambda row: to_decimal(row["learning_priority_score"]) or Decimal("0"),
        reverse=True,
    )
    selected = targets[:limit]
    inserted = 0
    if persist:
        for target in selected:
            insert_learning_trade_target(session, target)
            inserted += 1
    return LearningTargetsResult(targets=selected, inserted=inserted, scanned=len(rows))


def latest_learning_target_rows(
    session: Session,
    *,
    limit: int = 25,
) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(LearningTradeTarget)
            .order_by(
                desc(LearningTradeTarget.generated_at),
                desc(LearningTradeTarget.learning_priority_score),
                desc(LearningTradeTarget.id),
            )
            .limit(limit)
        )
    )
    return [
        {
            "ticker": row.ticker,
            "model_name": row.model_name,
            "category": row.category,
            "settlement_speed_score": row.settlement_speed_score,
            "learning_priority_score": row.learning_priority_score,
            "reason": row.reason,
            "generated_at": row.generated_at.isoformat(),
        }
        for row in rows
    ]


def _too_long(minutes: Decimal | None, settings: Settings) -> bool:
    if not settings.learning_prioritize_fast_settlement or minutes is None:
        return False
    now = utc_now().astimezone(UTC)
    del now
    return minutes > Decimal(settings.learning_max_days_to_settlement * 1440)


def _log_target_rejection(
    session: Session,
    *,
    ranking: MarketRanking,
    settings: Settings,
    category: str,
    spread: Decimal | None,
    liquidity: Decimal,
    minutes: Decimal | None,
    reason_override: str | None = None,
    raw_extra: dict[str, Any] | None = None,
) -> None:
    if reason_override is not None:
        reason = reason_override
    elif spread is not None and spread > settings.learning_max_spread:
        reason = "wide_spread"
    elif liquidity < settings.learning_min_liquidity:
        reason = "low_liquidity"
    elif _too_long(minutes, settings):
        reason = "settlement_too_slow"
    else:
        reason = "confidence_too_low"
    raw_json = {
        "source": "learning_targets",
        "ranking_id": ranking.id,
        "category": category,
        "thresholds": {
            "max_days_to_settlement": settings.learning_max_days_to_settlement,
            "max_spread": str(settings.learning_max_spread),
            "min_liquidity": str(settings.learning_min_liquidity),
        },
    }
    if raw_extra:
        raw_json.update(raw_extra)
    insert_learning_rejection(
        session,
        {
            "ticker": ranking.ticker,
            "model_name": ranking.forecast_model,
            "rejected_at": utc_now(),
            "reason": reason,
            "edge": to_decimal(ranking.estimated_edge),
            "opportunity_score": to_decimal(ranking.opportunity_score),
            "spread": spread,
            "liquidity": liquidity,
            "settlement_eta_hours": minutes / Decimal("60") if minutes is not None else None,
            "raw_json": raw_json,
        },
    )


def category_priority_score(
    *,
    category: str,
    market_text: str,
    minutes_to_close: Decimal | None = None,
) -> Decimal:
    return _category_priority_score(
        category=category,
        market_text=market_text,
        minutes_to_close=minutes_to_close,
    )


def _category_priority_score(
    *,
    category: str,
    market_text: str,
    minutes_to_close: Decimal | None = None,
) -> Decimal:
    base = CATEGORY_PRIORITY.get(category, CATEGORY_PRIORITY["unknown"])
    if _is_deprioritized_market(market_text):
        return min(base, Decimal("15"))
    if category == "sports" and _is_multi_game_market(market_text):
        return min(base, Decimal("10"))
    if minutes_to_close is None:
        return min(base, Decimal("40"))
    if minutes_to_close > Decimal("10080"):
        return min(base, Decimal("15"))
    return base


def _is_deprioritized_market(market_text: str) -> bool:
    normalized = market_text.lower()
    return any(
        token in normalized
        for token in (
            "election",
            "president",
            "senate",
            "congress",
            "governor",
            "nominee",
            "2028",
            "2029",
            "2030",
        )
    )


def _is_multi_game_market(market_text: str) -> bool:
    normalized = market_text.lower()
    return any(
        token in normalized
        for token in (
            "parlay",
            "multi-game",
            "multigame",
            "games",
            "teams",
            "playoffs",
            "series",
        )
    )


def _target_reason(
    ranking: MarketRanking,
    category: str,
    speed: Decimal,
    priority: Decimal,
) -> str:
    return (
        f"{ranking.ticker} is a {category} Learning Mode target with priority "
        f"{decimal_to_str(priority)} and settlement speed score {decimal_to_str(speed)}."
    )
