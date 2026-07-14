from collections.abc import Mapping
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import ModelLeaderboard
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def insert_leaderboard_row(
    session: Session,
    row: Mapping[str, Any],
) -> ModelLeaderboard:
    record = ModelLeaderboard(
        model_name=str(row["model_name"]),
        generated_at=row.get("generated_at") or utc_now(),
        forecast_count=int(row.get("forecast_count") or 0),
        evaluated_forecast_count=int(row.get("evaluated_forecast_count") or 0),
        paper_trade_count=int(row.get("paper_trade_count") or 0),
        settled_trade_count=int(row.get("settled_trade_count") or 0),
        brier_score=_decimal_string(row.get("brier_score")),
        log_loss=_decimal_string(row.get("log_loss")),
        win_rate=_decimal_string(row.get("win_rate")),
        total_pnl=_decimal_string(row.get("total_pnl")),
        roi_on_exposure=_decimal_string(row.get("roi_on_exposure")),
        avg_edge=_decimal_string(row.get("avg_edge")),
        max_drawdown=_decimal_string(row.get("max_drawdown")),
        notes=str(row.get("notes") or ""),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def latest_leaderboard_rows(session: Session, *, limit: int = 50) -> list[ModelLeaderboard]:
    return list(
        session.scalars(
            select(ModelLeaderboard)
            .order_by(desc(ModelLeaderboard.generated_at), ModelLeaderboard.model_name)
            .limit(limit)
        )
    )


def _decimal_string(value: Any) -> str | None:
    return decimal_to_str(value)

