from collections.abc import Mapping
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import ModelConfidenceScore
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def insert_model_confidence_score(
    session: Session,
    row: Mapping[str, Any],
) -> ModelConfidenceScore:
    record = ModelConfidenceScore(
        generated_at=row.get("generated_at") or utc_now(),
        model_name=str(row["model_name"]),
        category=str(row["category"]),
        lookback_days=int(row.get("lookback_days") or 0),
        forecast_count=int(row.get("forecast_count") or 0),
        evaluated_forecast_count=int(row.get("evaluated_forecast_count") or 0),
        paper_trade_count=int(row.get("paper_trade_count") or 0),
        settled_trade_count=int(row.get("settled_trade_count") or 0),
        brier_score=decimal_to_str(row.get("brier_score")),
        log_loss=decimal_to_str(row.get("log_loss")),
        win_rate=decimal_to_str(row.get("win_rate")),
        roi_on_exposure=decimal_to_str(row.get("roi_on_exposure")),
        total_pnl=decimal_to_str(row.get("total_pnl")),
        max_drawdown=decimal_to_str(row.get("max_drawdown")),
        sample_size_score=decimal_to_str(row.get("sample_size_score")) or "0",
        calibration_score=decimal_to_str(row.get("calibration_score")) or "0",
        profitability_score=decimal_to_str(row.get("profitability_score")) or "0",
        drawdown_score=decimal_to_str(row.get("drawdown_score")) or "0",
        confidence_score=decimal_to_str(row.get("confidence_score")) or "0",
        confidence_label=str(row.get("confidence_label") or "Needs More Data"),
        status=str(row.get("status") or "NEEDS_MORE_DATA"),
        notes=str(row.get("notes") or ""),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def latest_model_confidence_scores(
    session: Session,
    *,
    category: str | None = None,
    limit: int = 200,
) -> list[ModelConfidenceScore]:
    statement = select(ModelConfidenceScore).order_by(
        desc(ModelConfidenceScore.generated_at),
        desc(ModelConfidenceScore.id),
    )
    if category is not None:
        statement = statement.where(ModelConfidenceScore.category == category)
    records = list(session.scalars(statement.limit(limit)))
    selected: dict[tuple[str, str], ModelConfidenceScore] = {}
    for record in records:
        key = (record.category, record.model_name)
        if key not in selected:
            selected[key] = record
    return list(selected.values())


def confidence_rows_for_ui(
    session: Session,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    return [
        {
            "generated_at": row.generated_at.isoformat(),
            "model_name": row.model_name,
            "category": row.category,
            "settled_trade_count": row.settled_trade_count,
            "brier_score": row.brier_score,
            "win_rate": row.win_rate,
            "roi_on_exposure": row.roi_on_exposure,
            "confidence_score": row.confidence_score,
            "confidence_label": row.confidence_label,
            "status": row.status,
            "notes": row.notes,
        }
        for row in latest_model_confidence_scores(session, limit=limit)
    ]

