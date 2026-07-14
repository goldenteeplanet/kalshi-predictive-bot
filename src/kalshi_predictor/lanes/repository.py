from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    AutopilotMetric,
    AutopilotOpportunity,
    AutopilotPaperTrade,
    LearningMetric,
    LearningOpportunity,
    LearningPaperTrade,
    PaperOrder,
)
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def insert_learning_opportunity(
    session: Session,
    row: Mapping[str, Any],
) -> LearningOpportunity:
    record = LearningOpportunity(
        created_at=row.get("created_at") or row.get("detected_at") or utc_now(),
        ticker=str(row["ticker"]),
        model_name=str(row.get("model_name") or row.get("forecast_model")),
        side=_optional_str(row.get("side") or row.get("best_side")),
        price=decimal_to_str(row.get("price") or row.get("best_price")),
        forecast_probability=decimal_to_str(row.get("forecast_probability")),
        estimated_edge=decimal_to_str(row.get("estimated_edge")),
        opportunity_score=decimal_to_str(row.get("opportunity_score")),
        settlement_speed_score=decimal_to_str(row.get("settlement_speed_score")),
        status=str(row.get("status") or "OPEN"),
        source=str(row.get("source") or "learning"),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_autopilot_opportunity(
    session: Session,
    row: Mapping[str, Any],
) -> AutopilotOpportunity:
    raw = dict(row.get("raw_json") or row)
    record = AutopilotOpportunity(
        created_at=row.get("created_at") or row.get("detected_at") or utc_now(),
        autopilot_run_id=_optional_int(row.get("autopilot_run_id")),
        autopilot_cycle_id=_optional_int(row.get("autopilot_cycle_id")),
        ticker=str(row["ticker"]),
        model_name=str(row.get("model_name") or row.get("forecast_model")),
        side=_optional_str(row.get("side") or row.get("best_side")),
        price=decimal_to_str(row.get("price") or row.get("best_price")),
        forecast_probability=decimal_to_str(row.get("forecast_probability")),
        estimated_edge=decimal_to_str(row.get("estimated_edge")),
        opportunity_score=decimal_to_str(row.get("opportunity_score")),
        model_confidence_score=decimal_to_str(
            row.get("model_confidence_score") or raw.get("model_confidence_score")
        ),
        status=str(row.get("status") or "OPEN"),
        source=str(row.get("source") or "autopilot"),
        raw_json=encode_json(raw),
    )
    session.add(record)
    session.flush()
    return record


def insert_learning_paper_trade(
    session: Session,
    row: Mapping[str, Any],
) -> LearningPaperTrade:
    record = LearningPaperTrade(
        created_at=row.get("created_at") or utc_now(),
        paper_order_id=_optional_int(row.get("paper_order_id")),
        ticker=str(row["ticker"]),
        model_name=str(row["model_name"]),
        side=str(row["side"]),
        price=decimal_to_str(row.get("price")) or "0",
        quantity=int(row.get("quantity") or 0),
        edge=decimal_to_str(row.get("edge")),
        status=str(row.get("status") or "FILLED"),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_autopilot_paper_trade(
    session: Session,
    row: Mapping[str, Any],
) -> AutopilotPaperTrade:
    record = AutopilotPaperTrade(
        created_at=row.get("created_at") or utc_now(),
        autopilot_run_id=_optional_int(row.get("autopilot_run_id")),
        autopilot_cycle_id=_optional_int(row.get("autopilot_cycle_id")),
        paper_order_id=_optional_int(row.get("paper_order_id")),
        ticker=str(row["ticker"]),
        model_name=str(row["model_name"]),
        side=str(row["side"]),
        price=decimal_to_str(row.get("price")) or "0",
        quantity=int(row.get("quantity") or 0),
        edge=decimal_to_str(row.get("edge") or row.get("estimated_edge")),
        status=str(row.get("status") or "DRY_RUN"),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_learning_metric(
    session: Session,
    row: Mapping[str, Any],
) -> LearningMetric:
    record = LearningMetric(
        generated_at=row.get("generated_at") or utc_now(),
        window_days=int(row.get("window_days") or 30),
        opportunities_found=int(row.get("opportunities_found") or 0),
        paper_trades_created=int(row.get("paper_trades_created") or 0),
        settled_trade_count=int(row.get("settled_trade_count") or 0),
        win_rate=decimal_to_str(row.get("win_rate")),
        roi_on_exposure=decimal_to_str(row.get("roi_on_exposure")),
        total_pnl=decimal_to_str(row.get("total_pnl")),
        learning_confidence=decimal_to_str(row.get("learning_confidence")),
        notes=str(row.get("notes") or ""),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_autopilot_metric(
    session: Session,
    row: Mapping[str, Any],
) -> AutopilotMetric:
    record = AutopilotMetric(
        generated_at=row.get("generated_at") or utc_now(),
        window_days=int(row.get("window_days") or 30),
        opportunities_found=int(row.get("opportunities_found") or 0),
        dry_run_orders=int(row.get("dry_run_orders") or 0),
        settled_trade_count=int(row.get("settled_trade_count") or 0),
        win_rate=decimal_to_str(row.get("win_rate")),
        roi_on_exposure=decimal_to_str(row.get("roi_on_exposure")),
        total_pnl=decimal_to_str(row.get("total_pnl")),
        current_confidence=decimal_to_str(row.get("current_confidence")),
        notes=str(row.get("notes") or ""),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_learning_trade_for_order(
    session: Session,
    order: PaperOrder,
    *,
    source: str = "learning",
) -> LearningPaperTrade:
    return insert_learning_paper_trade(
        session,
        {
            "created_at": order.created_at,
            "paper_order_id": order.id,
            "ticker": order.ticker,
            "model_name": order.model_name,
            "side": order.side,
            "price": order.limit_price,
            "quantity": order.quantity,
            "edge": order.edge,
            "status": order.status,
            "raw_json": {
                "source": source,
                "paper_order_id": order.id,
                "reason": order.reason,
                "raw_decision": decode_json(order.raw_decision_json),
            },
        },
    )


def recent_learning_opportunities(
    session: Session,
    *,
    limit: int = 20,
) -> list[LearningOpportunity]:
    return list(
        session.scalars(
            select(LearningOpportunity)
            .order_by(desc(LearningOpportunity.created_at), desc(LearningOpportunity.id))
            .limit(limit)
        )
    )


def recent_autopilot_opportunities(
    session: Session,
    *,
    limit: int = 20,
) -> list[AutopilotOpportunity]:
    return list(
        session.scalars(
            select(AutopilotOpportunity)
            .order_by(desc(AutopilotOpportunity.created_at), desc(AutopilotOpportunity.id))
            .limit(limit)
        )
    )


def latest_learning_metric(session: Session) -> LearningMetric | None:
    return session.scalar(
        select(LearningMetric)
        .order_by(desc(LearningMetric.generated_at), desc(LearningMetric.id))
        .limit(1)
    )


def latest_autopilot_metric(session: Session) -> AutopilotMetric | None:
    return session.scalar(
        select(AutopilotMetric)
        .order_by(desc(AutopilotMetric.generated_at), desc(AutopilotMetric.id))
        .limit(1)
    )


def row_to_dict(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data: dict[str, Any] = {}
    for key in row.__mapper__.columns.keys():
        value = getattr(row, key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
        elif key.endswith("_json"):
            data[key] = decode_json(value) if isinstance(value, str) else value
        else:
            data[key] = value
    return data


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
