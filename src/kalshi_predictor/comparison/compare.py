from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.engine import run_backtest
from kalshi_predictor.data.schema import Forecast, Settlement
from kalshi_predictor.forecasting.registry import MODEL_NAMES


@dataclass(frozen=True)
class StrategyComparison:
    days: int
    rows: list[dict[str, Any]]


def compare_strategies(
    session: Session,
    *,
    days: int = 30,
    strategy_name: str = "paper_v1",
    model_names: tuple[str, ...] = MODEL_NAMES,
) -> StrategyComparison:
    rows: list[dict[str, Any]] = []
    for model_name in model_names:
        forecast_count = _forecast_count(session, model_name)
        evaluated_count = _evaluated_forecast_count(session, model_name)
        result = run_backtest(
            session,
            model_name=model_name,
            strategy_name=strategy_name,
            days=days,
            persist=False,
        )
        summary = result.summary
        rows.append(
            {
                "model_name": model_name,
                "forecast_count": forecast_count,
                "evaluated_forecast_count": evaluated_count,
                "simulated_trades": summary["total_trades"],
                "win_rate": summary["win_rate"],
                "total_pnl": summary["total_pnl"],
                "roi": summary["roi_on_exposure"],
                "brier_score": summary.get("brier_score"),
                "log_loss": summary.get("log_loss"),
                "notes": _notes(forecast_count, evaluated_count, summary["total_trades"]),
            }
        )
    return StrategyComparison(days=days, rows=rows)


def _forecast_count(session: Session, model_name: str) -> int:
    value = session.scalar(
        select(func.count()).select_from(Forecast).where(Forecast.model_name == model_name)
    )
    return int(value or 0)


def _evaluated_forecast_count(session: Session, model_name: str) -> int:
    value = session.scalar(
        select(func.count())
        .select_from(Forecast)
        .join(Settlement, Forecast.ticker == Settlement.ticker)
        .where(Forecast.model_name == model_name)
    )
    return int(value or 0)


def _notes(forecast_count: int, evaluated_count: int, trade_count: int) -> str:
    if forecast_count == 0:
        return "No forecasts found for this model."
    if evaluated_count == 0:
        return "Forecasts exist, but none have settlement rows yet."
    if trade_count == 0:
        return "Evaluated forecasts did not clear paper strategy edge filters."
    return "Compared using stored settled forecasts only."

