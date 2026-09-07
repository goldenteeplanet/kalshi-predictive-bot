from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    CanonicalEvaluation,
    Forecast,
    Market,
    MarketSnapshot,
    PaperOrder,
    Settlement,
    ShadowDecision,
)
from kalshi_predictor.phase4cd.domain import (
    SHADOW,
    correlation_cluster_id,
    deterministic_id,
    independent_event_id,
    score_probability,
    settlement_value,
    validate_point_in_time,
)
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class ShadowCaptureResult:
    scanned: int
    created: int
    skipped: int


@dataclass(frozen=True)
class ShadowReconcileResult:
    decisions: int
    settled: int
    evaluated: int


def capture_shadow_decisions(
    session: Session,
    *,
    model: str | None = None,
    limit: int = 1000,
    max_age_minutes: int = 30,
    slippage: Decimal = Decimal("0.01"),
    settings: Settings | None = None,
) -> ShadowCaptureResult:
    if limit < 1 or max_age_minutes < 1:
        raise ValueError("limit and max_age_minutes must be positive")
    resolved = settings or get_settings()
    paper_before = _paper_count(session)
    query = select(Forecast, Market).join(Market, Market.ticker == Forecast.ticker)
    if model:
        query = query.where(Forecast.model_name == model)
    rows = list(
        session.execute(
            query.order_by(Forecast.forecasted_at.desc(), Forecast.id.desc()).limit(limit)
        )
    )
    now = utc_now()
    created = 0
    skipped = 0
    for forecast, market in rows:
        snapshot = session.scalar(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.ticker == forecast.ticker,
                MarketSnapshot.captured_at <= forecast.forecasted_at,
            )
            .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
            .limit(1)
        )
        candidate = _candidate(
            forecast=forecast,
            market=market,
            snapshot=snapshot,
            now=now,
            max_age=timedelta(minutes=max_age_minutes),
            fee=resolved.paper_default_fee_per_contract,
            slippage=slippage,
        )
        if candidate is None:
            skipped += 1
            continue
        if session.get(ShadowDecision, candidate.decision_id) is None:
            session.add(candidate)
            created += 1
    session.flush()
    if _paper_count(session) != paper_before:
        session.rollback()
        raise RuntimeError("SHADOW_PAPER_LANE_VIOLATION")
    session.commit()
    return ShadowCaptureResult(scanned=len(rows), created=created, skipped=skipped)


def reconcile_shadow_settlements(session: Session, *, limit: int = 5000) -> ShadowReconcileResult:
    paper_before = _paper_count(session)
    rows = list(
        session.scalars(
            select(ShadowDecision)
            .where(ShadowDecision.settlement_result.is_(None))
            .order_by(ShadowDecision.decision_time, ShadowDecision.decision_id)
            .limit(limit)
        )
    )
    settled = 0
    evaluated = 0
    for decision in rows:
        settlement = session.get(Settlement, decision.ticker)
        outcome = settlement_value(settlement.result if settlement else None)
        if settlement is None or outcome is None or settlement.settled_at is None:
            continue
        validate_point_in_time(
            feature_timestamp=decision.forecast_timestamp,
            source_timestamp=decision.forecast_timestamp,
            snapshot_timestamp=decision.snapshot_timestamp,
            decision_timestamp=decision.decision_time,
            settlement_timestamp=settlement.settled_at,
        )
        settled += 1
        price = Decimal(decision.executable_price)
        fee = Decimal(decision.fees)
        slippage = Decimal(decision.slippage)
        won = (decision.side == "BUY_YES" and outcome == 1) or (
            decision.side == "BUY_NO" and outcome == 0
        )
        pnl = (Decimal("1") if won else Decimal("0")) - price - fee - slippage
        probability = Decimal(decision.forecast)
        brier, log_loss = score_probability(probability, outcome)
        decision.settlement_timestamp = settlement.settled_at
        decision.settlement_result = settlement.result
        decision.hypothetical_pnl = str(pnl)
        evaluation_id = deterministic_id("canonical-evaluation-v1", SHADOW, decision.decision_id)
        if session.get(CanonicalEvaluation, evaluation_id) is None:
            session.add(
                CanonicalEvaluation(
                    evaluation_id=evaluation_id,
                    source_lane=SHADOW,
                    market_ticker=decision.ticker,
                    event_ticker=decision.event_ticker,
                    series_ticker=decision.series_ticker,
                    model=decision.model,
                    model_version=decision.model_version,
                    decision_timestamp=decision.decision_time,
                    forecast_timestamp=decision.forecast_timestamp,
                    forecast_probability=decision.forecast,
                    snapshot_timestamp=decision.snapshot_timestamp,
                    executable_price=decision.executable_price,
                    spread=decision.spread,
                    liquidity=decision.liquidity,
                    gross_edge=decision.gross_edge,
                    fees=decision.fees,
                    slippage=decision.slippage,
                    net_edge=decision.net_ev,
                    risk_result=decision.risk_result,
                    settlement_timestamp=settlement.settled_at,
                    settlement_result=settlement.result or "",
                    realized_or_simulated_pnl=str(pnl),
                    brier_contribution=str(brier),
                    log_loss_contribution=str(log_loss),
                    independent_event_id=decision.independent_event_id,
                    correlation_cluster_id=decision.correlation_cluster_id,
                    provenance=decision.provenance,
                    created_at=utc_now(),
                )
            )
            evaluated += 1
    session.flush()
    if _paper_count(session) != paper_before:
        session.rollback()
        raise RuntimeError("SHADOW_PAPER_LANE_VIOLATION")
    session.commit()
    return ShadowReconcileResult(decisions=len(rows), settled=settled, evaluated=evaluated)


def _candidate(
    *,
    forecast: Forecast,
    market: Market,
    snapshot: MarketSnapshot | None,
    now,
    max_age: timedelta,
    fee: Decimal,
    slippage: Decimal,
) -> ShadowDecision | None:
    if snapshot is None or not market.rules_primary or market.status not in {"open", "active"}:
        return None
    if _as_utc(now) - _as_utc(forecast.forecasted_at) > max_age or _as_utc(
        now
    ) - _as_utc(snapshot.captured_at) > max_age:
        return None
    try:
        probability = Decimal(forecast.yes_probability)
        yes_price = _price(snapshot.best_yes_ask or snapshot.yes_ask_dollars)
        no_price = _price(snapshot.best_no_ask or snapshot.no_ask_dollars)
    except (InvalidOperation, ValueError, TypeError):
        return None
    choices: list[tuple[Decimal, str, Decimal]] = []
    if yes_price is not None:
        choices.append((probability - yes_price, "BUY_YES", yes_price))
    if no_price is not None:
        choices.append(((Decimal("1") - probability) - no_price, "BUY_NO", no_price))
    if not choices:
        return None
    gross, side, price = max(choices, key=lambda item: item[0])
    net = gross - fee - slippage
    liquidity = snapshot.open_interest_fp or snapshot.volume_fp
    if net <= 0 or liquidity in {None, "", "0", "0.0"}:
        return None
    independent_id = independent_event_id(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        series_ticker=market.series_ticker,
    )
    decision_id = deterministic_id(
        "canonical-shadow-v1", forecast.id, snapshot.id, side, str(price)
    )
    return ShadowDecision(
        decision_id=decision_id,
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        series_ticker=market.series_ticker,
        model=forecast.model_name,
        model_version=forecast.model_name.rsplit("_", 1)[-1],
        forecast_id=forecast.id,
        forecast=str(probability),
        forecast_timestamp=forecast.forecasted_at,
        snapshot_timestamp=snapshot.captured_at,
        side=side,
        executable_price=str(price),
        spread=snapshot.spread,
        liquidity=liquidity,
        gross_edge=str(gross),
        fees=str(fee),
        slippage=str(slippage),
        net_ev=str(net),
        risk_result="RESEARCH_PASS",
        paper_eligible=0,
        decision_time=forecast.forecasted_at,
        independent_event_id=independent_id,
        correlation_cluster_id=correlation_cluster_id(
            independent_id=independent_id, series_ticker=market.series_ticker
        ),
        settlement_timestamp=None,
        settlement_result=None,
        hypothetical_pnl=None,
        provenance=json.dumps(
            {
                "forecast_id": forecast.id,
                "snapshot_id": snapshot.id,
                "quality_gates": [
                    "VALID_SEMANTICS",
                    "FRESH_SNAPSHOT",
                    "FRESH_MODEL_INPUTS",
                    "VALID_EXECUTABLE_PRICE",
                    "POSITIVE_NET_EV",
                    "REASONABLE_LIQUIDITY",
                    "VALID_SETTLEMENT_SEMANTICS",
                ],
                "guarded_tables_written": False,
            },
            sort_keys=True,
        ),
        created_at=utc_now(),
    )


def _price(value: str | None) -> Decimal | None:
    if value is None:
        return None
    price = Decimal(value)
    return price if Decimal("0") < price < Decimal("1") else None


def _paper_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(PaperOrder)) or 0)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
