from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    LearningCycle,
    LearningRejectionLog,
    LearningRun,
    LearningTradeTarget,
)
from kalshi_predictor.learning.config import learning_config_payload
from kalshi_predictor.learning.safety import settled_paper_trade_count
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def create_learning_run(session: Session, settings: Settings) -> LearningRun:
    run = LearningRun(
        started_at=utc_now(),
        completed_at=None,
        status="RUNNING",
        cycles_completed=0,
        target_settled_trades=settings.learning_target_settled_trades,
        starting_settled_trades=settled_paper_trade_count(session),
        ending_settled_trades=0,
        paper_trades_created=0,
        settlements_synced=0,
        config_json=encode_json(dict(learning_config_payload(settings))),
        summary_json=None,
        notes="Paper-only Learning Mode run.",
    )
    session.add(run)
    session.flush()
    return run


def complete_learning_run(
    session: Session,
    run: LearningRun,
    *,
    status: str,
    cycles_completed: int,
    paper_trades_created: int,
    settlements_synced: int,
    summary: Mapping[str, Any],
) -> LearningRun:
    run.status = status
    run.completed_at = utc_now()
    run.cycles_completed = cycles_completed
    run.paper_trades_created = paper_trades_created
    run.settlements_synced = settlements_synced
    run.ending_settled_trades = settled_paper_trade_count(session)
    run.summary_json = encode_json(dict(summary))
    session.add(run)
    session.flush()
    return run


def create_learning_cycle(
    session: Session,
    *,
    run_id: int,
    cycle_number: int,
) -> LearningCycle:
    cycle = LearningCycle(
        learning_run_id=run_id,
        cycle_number=cycle_number,
        started_at=utc_now(),
        completed_at=None,
        status="RUNNING",
        markets_scanned=0,
        forecasts_generated=0,
        opportunities_found=0,
        paper_trades_created=0,
        settlements_synced=0,
        settled_paper_trades_total=settled_paper_trade_count(session),
        errors_json=None,
        summary_json=None,
    )
    session.add(cycle)
    session.flush()
    return cycle


def complete_learning_cycle(
    session: Session,
    cycle: LearningCycle,
    *,
    status: str,
    markets_scanned: int,
    forecasts_generated: int,
    opportunities_found: int,
    paper_trades_created: int,
    settlements_synced: int,
    errors: list[dict[str, Any]],
    summary: Mapping[str, Any],
) -> LearningCycle:
    cycle.status = status
    cycle.completed_at = utc_now()
    cycle.markets_scanned = markets_scanned
    cycle.forecasts_generated = forecasts_generated
    cycle.opportunities_found = opportunities_found
    cycle.paper_trades_created = paper_trades_created
    cycle.settlements_synced = settlements_synced
    cycle.settled_paper_trades_total = settled_paper_trade_count(session)
    cycle.errors_json = encode_json(errors)
    cycle.summary_json = encode_json(dict(summary))
    session.add(cycle)
    session.flush()
    return cycle


def insert_learning_trade_target(
    session: Session,
    row: Mapping[str, Any],
) -> LearningTradeTarget:
    target = LearningTradeTarget(
        generated_at=row.get("generated_at") or utc_now(),
        ticker=str(row["ticker"]),
        model_name=str(row["model_name"]),
        category=str(row["category"]),
        settlement_speed_score=decimal_to_str(row.get("settlement_speed_score")) or "0",
        learning_priority_score=decimal_to_str(row.get("learning_priority_score")) or "0",
        reason=str(row.get("reason") or ""),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(target)
    session.flush()
    return target


def insert_learning_rejection(
    session: Session,
    row: Mapping[str, Any],
) -> LearningRejectionLog:
    rejection = LearningRejectionLog(
        ticker=str(row["ticker"]),
        model_name=str(row["model_name"]),
        rejected_at=row.get("rejected_at") or utc_now(),
        reason=str(row["reason"]),
        edge=decimal_to_str(row.get("edge")),
        opportunity_score=decimal_to_str(row.get("opportunity_score")),
        spread=decimal_to_str(row.get("spread")),
        liquidity=decimal_to_str(row.get("liquidity")),
        settlement_eta_hours=decimal_to_str(row.get("settlement_eta_hours")),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(rejection)
    session.flush()
    return rejection


def recent_learning_runs(session: Session, *, limit: int = 10) -> list[LearningRun]:
    return list(
        session.scalars(
            select(LearningRun)
            .order_by(desc(LearningRun.started_at), desc(LearningRun.id))
            .limit(limit)
        )
    )


def recent_learning_cycles(session: Session, *, limit: int = 20) -> list[LearningCycle]:
    return list(
        session.scalars(
            select(LearningCycle)
            .order_by(desc(LearningCycle.started_at), desc(LearningCycle.id))
            .limit(limit)
        )
    )


def recent_learning_targets(session: Session, *, limit: int = 50) -> list[LearningTradeTarget]:
    return list(
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


def recent_learning_rejections(
    session: Session,
    *,
    limit: int = 100,
) -> list[LearningRejectionLog]:
    return list(
        session.scalars(
            select(LearningRejectionLog)
            .order_by(desc(LearningRejectionLog.rejected_at), desc(LearningRejectionLog.id))
            .limit(limit)
        )
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
