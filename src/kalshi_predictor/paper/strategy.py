from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, aliased

from kalshi_predictor.active_universe import is_ticker_eligible_for_new_forecasts
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    Forecast,
    LearningTradeTarget,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
)
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.learning.duplicates import candidate_key, is_duplicate_candidate
from kalshi_predictor.learning.repository import insert_learning_rejection
from kalshi_predictor.paper.ledger import (
    get_existing_order_for_forecast,
    get_latest_forecast_per_ticker,
    get_latest_snapshot_for_ticker,
    get_position,
    open_order_count,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, PaperDecision, StrategyResult
from kalshi_predictor.phase3ak import (
    NOT_MULTILEG,
    build_multi_leg_component_provenance,
    multi_leg_learning_eligibility,
    phase3ak_learning_rejection_reason,
)
from kalshi_predictor.position_sizing.service import ensure_paper_decision_sized
from kalshi_predictor.utils.decimals import ONE_DOLLAR, to_decimal


def generate_paper_decisions(
    session: Session,
    *,
    settings: Settings | None = None,
    model_name: str | None = None,
) -> StrategyResult:
    base_settings = settings or get_settings()
    resolved_settings = learning_paper_settings(base_settings)
    forecasts = get_latest_forecast_per_ticker(session, model_name=model_name)
    decisions: list[PaperDecision] = []
    skipped_due_to_edge = 0
    skipped_due_to_risk_limits = 0
    duplicates_skipped = 0
    current_open_orders = open_order_count(session)
    daily_trade_count = _daily_paper_trade_count(session) if resolved_settings.learning_mode else 0
    learning_cycle_target = _learning_cycle_target(resolved_settings)
    forecast_keys = [(forecast.ticker, forecast.model_name) for forecast in forecasts]
    rankings = (
        _latest_rankings_by_ticker(
            session,
            model_name=model_name,
            forecast_keys=forecast_keys,
        )
        if resolved_settings.learning_mode
        else {}
    )
    learning_targets = (
        _latest_targets_by_ticker(
            session,
            model_name=model_name,
            forecast_keys=forecast_keys,
        )
        if resolved_settings.learning_mode
        else {}
    )
    if resolved_settings.learning_mode:
        forecasts = _sort_learning_forecasts(
            forecasts,
            rankings=rankings,
            targets=learning_targets,
        )[: max(1, resolved_settings.learning_candidate_scan_limit)]
    phase3ak_gates = (
        _phase3ak_gates_for_forecasts(session, forecasts)
        if resolved_settings.learning_mode
        else {}
    )
    selected_keys: set[tuple[str, str, str]] = set()

    for forecast in forecasts:
        if (
            resolved_settings.learning_mode
            and learning_cycle_target is not None
            and len(decisions) >= learning_cycle_target
        ):
            break
        ranking = _ranking_for_forecast(rankings, forecast)
        if not is_ticker_eligible_for_new_forecasts(session, forecast.ticker):
            skipped_due_to_risk_limits += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=None,
                reason="inactive_market",
                settings=resolved_settings,
                raw_extra={
                    "phase3as_gate": {
                        "status": "INELIGIBLE",
                        "reason": "closed_or_inactive_market",
                    }
                },
            )
            continue
        phase3ak_gate = phase3ak_gates.get(forecast.ticker)
        if phase3ak_gate is None:
            phase3ak_gate = multi_leg_learning_eligibility(session, forecast.ticker)
        if phase3ak_gate["status"] != "NOT_MULTILEG" and not phase3ak_gate["eligible"]:
            skipped_due_to_risk_limits += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=None,
                reason=phase3ak_learning_rejection_reason(phase3ak_gate),
                settings=resolved_settings,
                raw_extra={"phase3ak_gate": phase3ak_gate},
            )
            continue
        if (
            not resolved_settings.learning_mode
            and forecast.id is not None
            and get_existing_order_for_forecast(session, forecast.id)
        ):
            duplicates_skipped += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=None,
                reason="duplicate_trade",
                settings=resolved_settings,
            )
            continue

        snapshot = get_latest_snapshot_for_ticker(session, forecast.ticker)
        decision = _best_decision_for_forecast(forecast, snapshot, resolved_settings)
        if decision is None:
            skipped_due_to_edge += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=None,
                reason=_missing_decision_reason(snapshot),
                settings=resolved_settings,
            )
            continue
        if (
            resolved_settings.learning_mode
            and _learning_opportunity_kind(ranking, decision, resolved_settings) == "avoid"
        ):
            skipped_due_to_edge += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=decision,
                reason=_learning_avoid_reason(ranking, decision, resolved_settings),
                settings=resolved_settings,
            )
            continue
        if (
            resolved_settings.learning_mode
            and is_duplicate_candidate(
                session,
                ticker=decision.ticker,
                model_name=decision.model_name,
                side=decision.side,
                cooldown_hours=resolved_settings.learning_duplicate_cooldown_hours,
                pending_keys=selected_keys,
            )
        ):
            duplicates_skipped += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=decision,
                reason="duplicate_trade",
                settings=resolved_settings,
            )
            continue

        if current_open_orders + len(decisions) >= resolved_settings.paper_max_open_orders:
            skipped_due_to_risk_limits += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=decision,
                reason="position_limit",
                settings=resolved_settings,
            )
            continue
        if (
            resolved_settings.learning_mode
            and daily_trade_count + len(decisions)
            >= resolved_settings.learning_max_daily_paper_trades
        ):
            skipped_due_to_risk_limits += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=decision,
                reason="daily_cap",
                settings=resolved_settings,
            )
            continue

        decision = ensure_paper_decision_sized(
            session,
            decision,
            settings=resolved_settings,
        )
        if decision.quantity <= 0:
            skipped_due_to_risk_limits += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=decision,
                reason="position_limit",
                settings=resolved_settings,
            )
            continue

        position = get_position(session, forecast.ticker)
        yes_contracts = position.yes_contracts if position is not None else 0
        no_contracts = position.no_contracts if position is not None else 0
        if decision.side == BUY_YES:
            next_position = yes_contracts + decision.quantity
        else:
            next_position = no_contracts + decision.quantity
        if next_position > resolved_settings.paper_max_position_per_market:
            skipped_due_to_risk_limits += 1
            _log_learning_strategy_rejection(
                session,
                forecast=forecast,
                ranking=ranking,
                decision=decision,
                reason="position_limit",
                settings=resolved_settings,
            )
            continue

        selected_keys.add(
            candidate_key(
                ticker=decision.ticker,
                model_name=decision.model_name,
                side=decision.side,
            )
        )
        decisions.append(decision)

    return StrategyResult(
        forecasts_scanned=len(forecasts),
        decisions=decisions,
        skipped_due_to_edge=skipped_due_to_edge,
        skipped_due_to_risk_limits=skipped_due_to_risk_limits,
        duplicates_skipped=duplicates_skipped,
        candidate_scan_limit=(
            resolved_settings.learning_candidate_scan_limit
            if resolved_settings.learning_mode
            else None
        ),
    )


def _phase3ak_gates_for_forecasts(
    session: Session,
    forecasts: list[Forecast],
) -> dict[str, dict[str, object]]:
    tickers = list(dict.fromkeys(forecast.ticker for forecast in forecasts))
    if not tickers:
        return {}
    payload = build_multi_leg_component_provenance(
        session,
        tickers=tickers,
        include_single_leg=True,
    )
    gates: dict[str, dict[str, object]] = {}
    for row in payload.get("rows", []):
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        if not row.get("is_multi_leg"):
            gates[ticker] = {
                "ticker": ticker,
                "status": NOT_MULTILEG,
                "eligible": True,
                "reason": "single_leg_or_non_sports_market",
            }
            continue
        gates[ticker] = {
            "ticker": ticker,
            "status": row.get("learning_eligibility"),
            "eligible": bool(row.get("learning_eligible")),
            "reason": row.get("blocking_reason"),
            "component_status_counts": row.get("component_status_counts", {}),
            "snapshot_status": row.get("snapshot_status", {}),
        }
    for ticker in tickers:
        gates.setdefault(
            ticker,
            {
                "ticker": ticker,
                "status": NOT_MULTILEG,
                "eligible": True,
                "reason": "no_multi_leg_sports_components",
            },
        )
    return gates


def _learning_cycle_target(settings: Settings) -> int | None:
    if not settings.learning_mode:
        return None
    return max(1, settings.learning_min_trades_per_cycle, settings.learning_target_trades_per_cycle)


def _best_decision_for_forecast(
    forecast: Forecast,
    snapshot: MarketSnapshot | None,
    settings: Settings,
) -> PaperDecision | None:
    yes_probability = to_decimal(forecast.yes_probability)
    if yes_probability is None:
        return None

    candidates: list[PaperDecision] = []
    yes_ask = _first_decimal(forecast.best_yes_ask, snapshot.best_yes_ask if snapshot else None)
    if yes_ask is not None:
        yes_edge = yes_probability - yes_ask
        if yes_edge >= settings.paper_min_edge:
            candidates.append(
                _decision(
                    forecast=forecast,
                    side=BUY_YES,
                    probability=yes_probability,
                    market_price=yes_ask,
                    edge=yes_edge,
                    quantity=settings.paper_max_order_quantity,
                    reason=(
                        f"BUY_YES edge {yes_edge} meets threshold "
                        f"{settings.paper_min_edge}; model YES probability "
                        f"{yes_probability} exceeds YES ask {yes_ask}."
                        f"{_learning_reason_suffix(settings)}"
                    ),
                )
            )

    no_ask = _first_decimal(snapshot.best_no_ask if snapshot else None)
    if settings.paper_allow_buy_no and no_ask is not None:
        no_probability = ONE_DOLLAR - yes_probability
        no_edge = no_probability - no_ask
        if no_edge >= settings.paper_min_edge:
            candidates.append(
                _decision(
                    forecast=forecast,
                    side=BUY_NO,
                    probability=yes_probability,
                    market_price=no_ask,
                    edge=no_edge,
                    quantity=settings.paper_max_order_quantity,
                    reason=(
                        f"BUY_NO edge {no_edge} meets threshold "
                        f"{settings.paper_min_edge}; model NO probability "
                        f"{no_probability} exceeds NO ask {no_ask}."
                        f"{_learning_reason_suffix(settings)}"
                    ),
                )
            )

    if not candidates:
        return None
    return max(candidates, key=lambda decision: decision.edge)


def _decision(
    *,
    forecast: Forecast,
    side: str,
    probability: Decimal,
    market_price: Decimal,
    edge: Decimal,
    quantity: int,
    reason: str,
) -> PaperDecision:
    raw_decision_json = {
        "forecast_id": forecast.id,
        "forecasted_at": forecast.forecasted_at.isoformat(),
        "model_name": forecast.model_name,
        "side": side,
        "probability": str(probability),
        "market_price": str(market_price),
        "limit_price": str(market_price),
        "edge": str(edge),
        "quantity": quantity,
        "reason": reason,
        "strategy": "paper_edge_v1",
    }
    return PaperDecision(
        ticker=forecast.ticker,
        forecast_id=forecast.id,
        model_name=forecast.model_name,
        side=side,
        probability=probability,
        market_price=market_price,
        limit_price=market_price,
        edge=edge,
        quantity=quantity,
        reason=reason,
        raw_decision_json=raw_decision_json,
    )


def _latest_rankings_by_ticker(
    session: Session,
    *,
    model_name: str | None,
    forecast_keys: Iterable[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], MarketRanking]:
    tickers = _tickers_from_forecast_keys(forecast_keys)
    if forecast_keys is not None and not tickers:
        return {}
    statement = select(
        MarketRanking,
        func.row_number()
        .over(
            partition_by=(MarketRanking.ticker, MarketRanking.forecast_model),
            order_by=(desc(MarketRanking.ranked_at), desc(MarketRanking.id)),
        )
        .label("row_number"),
    )
    if model_name:
        statement = statement.where(MarketRanking.forecast_model == model_name)
    if tickers:
        statement = statement.where(MarketRanking.ticker.in_(tickers))
    ranked = statement.subquery()
    ranking = aliased(MarketRanking, ranked)
    return {
        (row.ticker, row.forecast_model): row
        for row in session.scalars(select(ranking).where(ranked.c.row_number == 1))
    }


def _latest_targets_by_ticker(
    session: Session,
    *,
    model_name: str | None,
    forecast_keys: Iterable[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], LearningTradeTarget]:
    tickers = _tickers_from_forecast_keys(forecast_keys)
    if forecast_keys is not None and not tickers:
        return {}
    statement = select(
        LearningTradeTarget,
        func.row_number()
        .over(
            partition_by=(LearningTradeTarget.ticker, LearningTradeTarget.model_name),
            order_by=(desc(LearningTradeTarget.generated_at), desc(LearningTradeTarget.id)),
        )
        .label("row_number"),
    )
    if model_name:
        statement = statement.where(LearningTradeTarget.model_name == model_name)
    if tickers:
        statement = statement.where(LearningTradeTarget.ticker.in_(tickers))
    ranked = statement.subquery()
    target = aliased(LearningTradeTarget, ranked)
    return {
        (row.ticker, row.model_name): row
        for row in session.scalars(select(target).where(ranked.c.row_number == 1))
    }


def _tickers_from_forecast_keys(
    forecast_keys: Iterable[tuple[str, str]] | None,
) -> list[str]:
    if forecast_keys is None:
        return []
    return sorted({ticker for ticker, _model_name in forecast_keys if ticker})


def _sort_learning_forecasts(
    forecasts: list[Forecast],
    *,
    rankings: dict[tuple[str, str], MarketRanking],
    targets: dict[tuple[str, str], LearningTradeTarget],
) -> list[Forecast]:
    return sorted(
        forecasts,
        key=lambda forecast: _learning_forecast_sort_key(
            forecast,
            rankings=rankings,
            targets=targets,
        ),
        reverse=True,
    )


def _learning_forecast_sort_key(
    forecast: Forecast,
    *,
    rankings: dict[tuple[str, str], MarketRanking],
    targets: dict[tuple[str, str], LearningTradeTarget],
) -> tuple[Decimal, Decimal, object]:
    key = (forecast.ticker, forecast.model_name)
    target = targets.get(key)
    ranking = rankings.get(key)
    priority = to_decimal(target.learning_priority_score if target is not None else None)
    score = to_decimal(ranking.opportunity_score if ranking is not None else None)
    if priority is None:
        priority = score or Decimal("0")
    return priority, score or Decimal("0"), forecast.forecasted_at


def _ranking_for_forecast(
    rankings: dict[tuple[str, str], MarketRanking],
    forecast: Forecast,
) -> MarketRanking | None:
    return rankings.get((forecast.ticker, forecast.model_name))


def _learning_opportunity_kind(
    ranking: MarketRanking | None,
    decision: PaperDecision,
    settings: Settings,
) -> str:
    if ranking is None:
        return "watchlist"
    if _ranking_market_data_rejection_reason(ranking) is not None:
        return "avoid"
    spread = to_decimal(ranking.spread)
    liquidity = to_decimal(ranking.liquidity) or Decimal("0")
    if spread is not None and spread > settings.learning_max_spread:
        return "avoid"
    if liquidity < settings.learning_min_liquidity:
        return "avoid"
    score = to_decimal(ranking.opportunity_score) or Decimal("0")
    ranking_edge = to_decimal(ranking.estimated_edge) or Decimal("0")
    edge = max(decision.edge, ranking_edge)
    if score >= Decimal("80") and edge >= Decimal("0.05"):
        return "strong"
    if score >= settings.learning_min_opportunity_score or edge >= settings.learning_min_edge:
        return "watchlist"
    return "avoid"


def _learning_avoid_reason(
    ranking: MarketRanking | None,
    decision: PaperDecision,
    settings: Settings,
) -> str:
    if ranking is None:
        return "missing_forecast"
    market_data_reason = _ranking_market_data_rejection_reason(ranking)
    if market_data_reason is not None:
        return market_data_reason
    spread = to_decimal(ranking.spread)
    liquidity = to_decimal(ranking.liquidity) or Decimal("0")
    if spread is not None and spread > settings.learning_max_spread:
        return "wide_spread"
    if liquidity < settings.learning_min_liquidity:
        return "low_liquidity"
    score = to_decimal(ranking.opportunity_score) or Decimal("0")
    ranking_edge = to_decimal(ranking.estimated_edge) or Decimal("0")
    edge = max(decision.edge, ranking_edge)
    if edge < settings.learning_min_edge:
        return "low_edge"
    if score < settings.learning_min_opportunity_score:
        return "low_score"
    return "confidence_too_low"


def _missing_decision_reason(snapshot: MarketSnapshot | None) -> str:
    if snapshot is None:
        return "missing_market_snapshot"
    if snapshot.best_yes_ask is None and snapshot.best_no_ask is None:
        return "missing_market_snapshot"
    return "low_edge"


def _ranking_market_data_rejection_reason(ranking: MarketRanking) -> str | None:
    if _first_positive_decimal(ranking.best_price, ranking.midpoint) is None:
        return "missing_market_snapshot"
    if _first_positive_decimal(ranking.spread) is None:
        return "missing_market_snapshot"
    if _first_positive_decimal(ranking.liquidity) is None:
        return "missing_liquidity"
    return None


def _log_learning_strategy_rejection(
    session: Session,
    *,
    forecast: Forecast,
    ranking: MarketRanking | None,
    decision: PaperDecision | None,
    reason: str,
    settings: Settings,
    raw_extra: dict[str, object] | None = None,
) -> None:
    if not settings.learning_mode:
        return
    edge = (
        decision.edge
        if decision is not None
        else to_decimal(ranking.estimated_edge if ranking is not None else None)
    )
    minutes = to_decimal(ranking.time_to_close_minutes if ranking is not None else None)
    settlement_eta_hours = minutes / Decimal("60") if minutes is not None else None
    raw_json = {
        "source": "paper_strategy",
        "forecast_id": forecast.id,
        "ranking_id": ranking.id if ranking is not None else None,
        "decision": decision.raw_decision_json if decision is not None else None,
        "thresholds": {
            "min_edge": str(settings.learning_min_edge),
            "min_score": str(settings.learning_min_opportunity_score),
            "max_spread": str(settings.learning_max_spread),
            "min_liquidity": str(settings.learning_min_liquidity),
        },
    }
    if raw_extra:
        raw_json.update(raw_extra)
    insert_learning_rejection(
        session,
        {
            "ticker": forecast.ticker,
            "model_name": forecast.model_name,
            "reason": reason,
            "edge": edge,
            "opportunity_score": (
                to_decimal(ranking.opportunity_score) if ranking is not None else None
            ),
            "spread": to_decimal(ranking.spread) if ranking is not None else None,
            "liquidity": to_decimal(ranking.liquidity) if ranking is not None else None,
            "settlement_eta_hours": settlement_eta_hours,
            "raw_json": raw_json,
        },
    )


def _daily_paper_trade_count(session: Session) -> int:
    from kalshi_predictor.utils.time import utc_now

    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.scalar(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.created_at >= today)
        )
        or 0
    )


def _learning_reason_suffix(settings: Settings) -> str:
    if not settings.learning_mode:
        return ""
    return " Learning Mode paper trade created to grow settled sample size."


def _first_decimal(*values: object) -> Decimal | None:
    for value in values:
        decimal_value = to_decimal(value)
        if decimal_value is not None:
            return decimal_value
    return None


def _first_positive_decimal(*values: object) -> Decimal | None:
    for value in values:
        decimal_value = to_decimal(value)
        if decimal_value is not None and decimal_value > 0:
            return decimal_value
    return None
