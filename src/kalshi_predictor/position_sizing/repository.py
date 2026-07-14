import json
import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import PaperOrder, PositionSizingDecisionLog
from kalshi_predictor.position_sizing.sizer import PositionSizingDecision
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now

logger = logging.getLogger(__name__)


def insert_position_sizing_decision(
    session: Session,
    decision: PositionSizingDecision,
    *,
    ticker: str,
    model_name: str | None,
    strategy_id: str | None,
    instrument: str | None,
    trade_intent_id: str | None,
    order_correlation_id: str | None,
    raw: Mapping[str, Any] | None = None,
) -> PositionSizingDecisionLog:
    payload = {
        **decision.as_dict(),
        "ticker": ticker,
        "model_name": model_name,
        "strategy_id": strategy_id,
        "instrument": instrument,
        "trade_intent_id": trade_intent_id,
        "order_correlation_id": order_correlation_id,
        "raw": dict(raw or {}),
    }
    record = PositionSizingDecisionLog(
        decision_timestamp=decision.decision_timestamp,
        created_at=utc_now(),
        version=decision.version,
        mode=decision.mode.value,
        strategy_id=strategy_id,
        instrument=instrument,
        ticker=ticker,
        model_name=model_name,
        trade_intent_id=trade_intent_id,
        order_correlation_id=order_correlation_id,
        paper_order_id=None,
        tier=decision.tier.value,
        composite_score=decimal_to_str(decision.composite_score) or "0",
        proposed_contracts=decision.proposed_contracts,
        live_candidate_contracts=decision.live_candidate_contracts,
        executed_contracts=decision.executed_contracts,
        factor_scores_json=encode_json(decision.factor_scores),
        factor_weights_json=encode_json(decision.factor_weights),
        adjusted_historical_accuracy=(
            decimal_to_str(decision.adjusted_historical_accuracy) or "0"
        ),
        historical_sample_size=decision.historical_sample_size,
        drawdown_utilization=decimal_to_str(decision.drawdown_utilization) or "0",
        caps_json=encode_json(decision.caps),
        limiting_factors_json=encode_json(list(decision.limiting_factors)),
        reason_codes_json=encode_json(list(decision.reason_codes)),
        fallback_used=int(decision.fallback_used),
        raw_json=encode_json(payload),
    )
    session.add(record)
    session.flush()
    from kalshi_predictor.memory.capture import capture_position_sizing_decision

    capture_position_sizing_decision(session, record)
    logger.info(
        "position_sizing_decision",
        extra={
            "position_sizing": {
                **payload,
                "position_sizing_decision_id": record.id,
            },
        },
    )
    return record


def attach_position_sizing_decision_to_order(
    session: Session,
    *,
    decision_id: int,
    order: PaperOrder,
) -> PositionSizingDecisionLog | None:
    record = session.get(PositionSizingDecisionLog, decision_id)
    if record is None:
        return None
    record.paper_order_id = order.id
    record.order_correlation_id = record.order_correlation_id or (
        f"paper_order:{order.id}" if order.id is not None else None
    )
    session.add(record)
    session.flush()
    return record


def latest_position_sizing_decisions(
    session: Session,
    *,
    limit: int = 50,
) -> list[PositionSizingDecisionLog]:
    return list(
        session.scalars(
            select(PositionSizingDecisionLog)
            .order_by(
                desc(PositionSizingDecisionLog.decision_timestamp),
                desc(PositionSizingDecisionLog.id),
            )
            .limit(limit)
        )
    )


def position_sizing_decision_to_dict(row: PositionSizingDecisionLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "decision_timestamp": row.decision_timestamp.isoformat(),
        "created_at": row.created_at.isoformat(),
        "version": row.version,
        "mode": row.mode,
        "strategy_id": row.strategy_id,
        "instrument": row.instrument,
        "ticker": row.ticker,
        "model_name": row.model_name,
        "trade_intent_id": row.trade_intent_id,
        "order_correlation_id": row.order_correlation_id,
        "paper_order_id": row.paper_order_id,
        "tier": row.tier,
        "composite_score": row.composite_score,
        "proposed_contracts": row.proposed_contracts,
        "live_candidate_contracts": row.live_candidate_contracts,
        "executed_contracts": row.executed_contracts,
        "factor_scores": decode_json(row.factor_scores_json),
        "factor_weights": decode_json(row.factor_weights_json),
        "adjusted_historical_accuracy": row.adjusted_historical_accuracy,
        "historical_sample_size": row.historical_sample_size,
        "drawdown_utilization": row.drawdown_utilization,
        "caps": decode_json(row.caps_json),
        "limiting_factors": _decode_json_value(row.limiting_factors_json),
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
