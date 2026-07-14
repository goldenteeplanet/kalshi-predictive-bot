from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.engine import (
    AdvancedRiskDecision,
    AdvancedRiskRequest,
)
from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    AdvancedRiskHighWaterMark,
    AdvancedRiskReservation,
    PaperOrder,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

logger = logging.getLogger(__name__)
_RESERVATION_LOCK = threading.RLock()

RESERVATION_RESERVED = "RESERVED"
RESERVATION_ATTACHED = "ATTACHED_TO_ORDER"
RESERVATION_FILLED = "FILLED"
RESERVATION_RELEASED = "RELEASED"


def reservation_lock() -> threading.RLock:
    return _RESERVATION_LOCK


def insert_advanced_risk_decision(
    session: Session,
    decision: AdvancedRiskDecision,
    request: AdvancedRiskRequest,
    *,
    ticker: str,
    position_sizing_decision_id: int | None,
    raw: Mapping[str, Any] | None = None,
) -> AdvancedRiskDecisionLog:
    payload = {
        **decision.as_dict(),
        "ticker": ticker,
        "strategy_id": request.strategy_id,
        "model_id": request.model_id,
        "category_id": request.category_id,
        "instrument_id": request.instrument_id,
        "correlation_group_id": request.correlation_group_id,
        "trade_intent_id": request.trade_intent_id,
        "order_correlation_id": request.order_correlation_id,
        "position_sizing_decision_id": position_sizing_decision_id,
        "raw": dict(raw or {}),
    }
    record = AdvancedRiskDecisionLog(
        decision_timestamp=decision.decision_timestamp,
        created_at=utc_now(),
        version=decision.version,
        mode=decision.mode.value,
        action=decision.action.value,
        strategy_id=request.strategy_id,
        model_id=request.model_id,
        category_id=request.category_id,
        instrument_id=request.instrument_id,
        correlation_group_id=request.correlation_group_id,
        ticker=ticker,
        trade_intent_id=request.trade_intent_id,
        order_correlation_id=request.order_correlation_id,
        position_sizing_decision_id=position_sizing_decision_id,
        paper_order_id=None,
        reservation_id=decision.reservation_id,
        phase_3m_tier=decision.phase_3m_tier,
        phase_3m_proposed_contracts=decision.phase_3m_proposed_contracts,
        live_candidate_contracts=decision.live_candidate_contracts,
        executed_contracts=decision.executed_contracts,
        risk_per_contract=decimal_to_str(decision.risk_per_contract) or "0",
        planned_trade_risk=decimal_to_str(decision.planned_trade_risk) or "0",
        raw_caps_json=encode_json(decision.raw_caps),
        bucketed_caps_json=encode_json(decision.bucketed_caps),
        limiting_factors_json=encode_json(list(decision.limiting_factors)),
        hard_blocks_json=encode_json(list(decision.hard_blocks)),
        reason_codes_json=encode_json(list(decision.reason_codes)),
        fallback_used=int(decision.fallback_used),
        raw_json=encode_json(payload),
    )
    session.add(record)
    session.flush()
    from kalshi_predictor.memory.capture import capture_advanced_risk_decision

    capture_advanced_risk_decision(session, record)
    logger.info(
        "advanced_risk_decision",
        extra={"advanced_risk": {**payload, "advanced_risk_decision_id": record.id}},
    )
    return record


def reserve_advanced_risk(
    session: Session,
    *,
    decision_record: AdvancedRiskDecisionLog,
    decision: AdvancedRiskDecision,
    request: AdvancedRiskRequest,
    ticker: str,
) -> AdvancedRiskReservation | None:
    if decision.executed_contracts <= 0:
        return None
    existing = session.scalar(
        select(AdvancedRiskReservation)
        .where(AdvancedRiskReservation.trade_intent_id == request.trade_intent_id)
        .limit(1)
    )
    if existing is not None:
        decision_record.reservation_id = existing.id
        session.add(decision_record)
        session.flush()
        return existing
    reserved_risk = decision.risk_per_contract * Decimal(decision.executed_contracts)
    reservation = AdvancedRiskReservation(
        trade_intent_id=request.trade_intent_id,
        order_correlation_id=request.order_correlation_id,
        decision_id=decision_record.id,
        paper_order_id=None,
        status=RESERVATION_RESERVED,
        reserved_at=utc_now(),
        released_at=None,
        ticker=ticker,
        model_id=request.model_id,
        category_id=request.category_id,
        instrument_id=request.instrument_id,
        correlation_group_id=request.correlation_group_id,
        quantity=decision.executed_contracts,
        risk_per_contract=decimal_to_str(decision.risk_per_contract) or "0",
        reserved_risk=decimal_to_str(reserved_risk) or "0",
        raw_json=encode_json(
            {
                "decision_id": decision_record.id,
                "trade_intent_id": request.trade_intent_id,
                "reason_codes": list(decision.reason_codes),
            }
        ),
    )
    session.add(reservation)
    session.flush()
    decision_record.reservation_id = reservation.id
    session.add(decision_record)
    session.flush()
    return reservation


def attach_advanced_risk_decision_to_order(
    session: Session,
    *,
    decision_id: int,
    order: PaperOrder,
) -> AdvancedRiskDecisionLog | None:
    record = session.get(AdvancedRiskDecisionLog, decision_id)
    if record is None:
        return None
    record.paper_order_id = order.id
    session.add(record)
    if record.reservation_id is not None:
        reservation = session.get(AdvancedRiskReservation, record.reservation_id)
        if reservation is not None:
            reservation.paper_order_id = order.id
            reservation.status = RESERVATION_ATTACHED
            session.add(reservation)
    session.flush()
    return record


def mark_reservation_filled_for_order(session: Session, order: PaperOrder) -> None:
    if order.id is None:
        return
    rows = session.scalars(
        select(AdvancedRiskReservation).where(AdvancedRiskReservation.paper_order_id == order.id)
    )
    for reservation in rows:
        reservation.status = RESERVATION_FILLED
        reservation.released_at = utc_now()
        session.add(reservation)
    session.flush()


def release_reservation_for_order(
    session: Session,
    order: PaperOrder,
    *,
    status: str = RESERVATION_RELEASED,
) -> None:
    if order.id is None:
        return
    rows = session.scalars(
        select(AdvancedRiskReservation).where(AdvancedRiskReservation.paper_order_id == order.id)
    )
    for reservation in rows:
        reservation.status = status
        reservation.released_at = utc_now()
        session.add(reservation)
    session.flush()


def active_unattached_reservations(session: Session) -> list[AdvancedRiskReservation]:
    return list(
        session.scalars(
            select(AdvancedRiskReservation)
            .where(AdvancedRiskReservation.status == RESERVATION_RESERVED)
            .where(AdvancedRiskReservation.paper_order_id.is_(None))
            .order_by(AdvancedRiskReservation.reserved_at, AdvancedRiskReservation.id)
        )
    )


def high_water_equity(session: Session, *, account_key: str, observed_equity: Decimal) -> Decimal:
    row = session.get(AdvancedRiskHighWaterMark, account_key)
    if row is None:
        row = AdvancedRiskHighWaterMark(
            account_key=account_key,
            high_water_equity=decimal_to_str(observed_equity) or "0",
            updated_at=utc_now(),
        )
        session.add(row)
        session.flush()
        return observed_equity
    current = to_decimal(row.high_water_equity) or Decimal("0")
    if observed_equity > current:
        row.high_water_equity = decimal_to_str(observed_equity) or "0"
        row.updated_at = utc_now()
        session.add(row)
        session.flush()
        return observed_equity
    return current


def advanced_risk_summary(session: Session) -> dict[str, Any]:
    decisions = list(
        session.scalars(
            select(AdvancedRiskDecisionLog)
            .order_by(
                desc(AdvancedRiskDecisionLog.decision_timestamp),
                desc(AdvancedRiskDecisionLog.id),
            )
            .limit(25)
        )
    )
    counts = {
        action: int(
            session.scalar(
                select(func.count())
                .select_from(AdvancedRiskDecisionLog)
                .where(AdvancedRiskDecisionLog.action == action)
            )
            or 0
        )
        for action in ("ALLOW", "REDUCE", "BLOCK")
    }
    active_reserved = session.scalar(
        select(func.coalesce(func.sum(AdvancedRiskReservation.quantity), 0)).where(
            AdvancedRiskReservation.status.in_((RESERVATION_RESERVED, RESERVATION_ATTACHED))
        )
    )
    return {
        "decision_count": sum(counts.values()),
        "allow_count": counts["ALLOW"],
        "reduce_count": counts["REDUCE"],
        "block_count": counts["BLOCK"],
        "active_reserved_contracts": int(active_reserved or 0),
        "latest_decisions": [advanced_risk_decision_to_dict(row) for row in decisions],
    }


def advanced_risk_decision_to_dict(row: AdvancedRiskDecisionLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "decision_timestamp": row.decision_timestamp.isoformat(),
        "created_at": row.created_at.isoformat(),
        "version": row.version,
        "mode": row.mode,
        "action": row.action,
        "ticker": row.ticker,
        "model_id": row.model_id,
        "category_id": row.category_id,
        "trade_intent_id": row.trade_intent_id,
        "paper_order_id": row.paper_order_id,
        "reservation_id": row.reservation_id,
        "phase_3m_tier": row.phase_3m_tier,
        "phase_3m_proposed_contracts": row.phase_3m_proposed_contracts,
        "live_candidate_contracts": row.live_candidate_contracts,
        "executed_contracts": row.executed_contracts,
        "risk_per_contract": row.risk_per_contract,
        "planned_trade_risk": row.planned_trade_risk,
        "raw_caps": decode_json(row.raw_caps_json),
        "bucketed_caps": decode_json(row.bucketed_caps_json),
        "limiting_factors": _decode_json_value(row.limiting_factors_json),
        "hard_blocks": _decode_json_value(row.hard_blocks_json),
        "reason_codes": _decode_json_value(row.reason_codes_json),
        "fallback_used": bool(row.fallback_used),
        "raw": decode_json(row.raw_json),
    }


def _decode_json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
