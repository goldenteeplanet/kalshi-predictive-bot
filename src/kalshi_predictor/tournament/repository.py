from collections.abc import Mapping
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    ModelDiagnostic,
    ModelTournamentResult,
    ModelTournamentRun,
    ModelWeight,
)
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def create_tournament_run(
    session: Session,
    *,
    name: str,
    days: int,
    config: Mapping[str, Any],
    notes: str,
) -> ModelTournamentRun:
    run = ModelTournamentRun(
        name=name,
        started_at=utc_now(),
        completed_at=None,
        days=days,
        config_json=encode_json(dict(config)),
        summary_json=None,
        notes=notes,
    )
    session.add(run)
    session.flush()
    return run


def complete_tournament_run(
    session: Session,
    run: ModelTournamentRun,
    *,
    summary: Mapping[str, Any],
) -> ModelTournamentRun:
    run.completed_at = utc_now()
    run.summary_json = encode_json(dict(summary))
    session.add(run)
    session.flush()
    return run


def insert_tournament_result(
    session: Session,
    row: Mapping[str, Any],
) -> ModelTournamentResult:
    record = ModelTournamentResult(
        tournament_run_id=int(row["tournament_run_id"]),
        model_name=str(row["model_name"]),
        category=str(row["category"]),
        forecast_count=int(row.get("forecast_count") or 0),
        evaluated_forecast_count=int(row.get("evaluated_forecast_count") or 0),
        simulated_trade_count=int(row.get("simulated_trade_count") or 0),
        settled_trade_count=int(row.get("settled_trade_count") or 0),
        brier_score=decimal_to_str(row.get("brier_score")),
        log_loss=decimal_to_str(row.get("log_loss")),
        win_rate=decimal_to_str(row.get("win_rate")),
        total_pnl=decimal_to_str(row.get("total_pnl")),
        roi_on_exposure=decimal_to_str(row.get("roi_on_exposure")),
        avg_edge=decimal_to_str(row.get("avg_edge")),
        max_drawdown=decimal_to_str(row.get("max_drawdown")),
        calibration_rank=_int_or_none(row.get("calibration_rank")),
        pnl_rank=_int_or_none(row.get("pnl_rank")),
        overall_rank=_int_or_none(row.get("overall_rank")),
        status=str(row.get("status") or "INSUFFICIENT_DATA"),
        notes=str(row.get("notes") or ""),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_model_weight(session: Session, row: Mapping[str, Any]) -> ModelWeight:
    record = ModelWeight(
        generated_at=row.get("generated_at") or utc_now(),
        model_name=str(row["model_name"]),
        category=str(row["category"]),
        weight=decimal_to_str(row.get("weight")) or "0",
        method=str(row.get("method") or "tournament_v1"),
        lookback_days=int(row.get("lookback_days") or 0),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_model_diagnostic(session: Session, row: Mapping[str, Any]) -> ModelDiagnostic:
    record = ModelDiagnostic(
        generated_at=row.get("generated_at") or utc_now(),
        model_name=str(row["model_name"]),
        category=str(row["category"]),
        diagnostic_type=str(row["diagnostic_type"]),
        metric_name=str(row["metric_name"]),
        metric_value=decimal_to_str(row.get("metric_value")),
        notes=str(row.get("notes") or ""),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def get_latest_model_weights(
    session: Session,
    *,
    category: str | None = None,
    limit: int = 200,
) -> list[ModelWeight]:
    statement = select(ModelWeight).order_by(desc(ModelWeight.generated_at), desc(ModelWeight.id))
    if category is not None:
        statement = statement.where(ModelWeight.category == category)
    records = list(session.scalars(statement.limit(limit)))
    selected: dict[tuple[str, str], ModelWeight] = {}
    for record in records:
        key = (record.category, record.model_name)
        if key not in selected:
            selected[key] = record
    return list(selected.values())


def get_latest_tournament_results(
    session: Session,
    *,
    limit: int = 200,
) -> list[ModelTournamentResult]:
    latest_run_id = session.scalar(
        select(ModelTournamentRun.id).order_by(desc(ModelTournamentRun.started_at)).limit(1)
    )
    if latest_run_id is None:
        return []
    return list(
        session.scalars(
            select(ModelTournamentResult)
            .where(ModelTournamentResult.tournament_run_id == latest_run_id)
            .order_by(ModelTournamentResult.category, ModelTournamentResult.overall_rank)
            .limit(limit)
        )
    )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
