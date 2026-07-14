from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    BacktestRun,
    BacktestTrade,
    Forecast,
    PaperOrder,
    Settlement,
)
from kalshi_predictor.evaluation.metrics import brier_score, log_loss
from kalshi_predictor.forecasting.registry import MODEL_NAMES
from kalshi_predictor.leaderboard.repository import insert_leaderboard_row
from kalshi_predictor.tournament.repository import get_latest_tournament_results
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class LeaderboardResult:
    generated_at: object
    days: int
    rows: list[dict[str, Any]]


def build_model_leaderboard(
    session: Session,
    *,
    days: int = 30,
    model_names: tuple[str, ...] = MODEL_NAMES,
    persist: bool = True,
) -> LeaderboardResult:
    generated_at = utc_now()
    start_time = generated_at - timedelta(days=days)
    tournament_context = _latest_tournament_context(session)
    rows: list[dict[str, Any]] = []
    for model_name in model_names:
        row = _leaderboard_row(
            session,
            model_name=model_name,
            generated_at=generated_at,
            start_time=start_time,
            tournament_context=tournament_context,
        )
        rows.append(row)
        if persist:
            insert_leaderboard_row(session, row)
    return LeaderboardResult(generated_at=generated_at, days=days, rows=rows)


def _leaderboard_row(
    session: Session,
    *,
    model_name: str,
    generated_at: object,
    start_time: object,
    tournament_context: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    forecasts = list(
        session.scalars(
            select(Forecast).where(
                Forecast.model_name == model_name,
                Forecast.forecasted_at >= start_time,
            )
        )
    )
    evaluated = _evaluated_forecasts(session, forecasts)
    backtest_trades = _backtest_trades(session, model_name=model_name, start_time=start_time)
    paper_trade_count = _paper_trade_count(session, model_name)
    pnl_metrics = _pnl_metrics(backtest_trades)
    calibration = _calibration_metrics(evaluated)
    notes = _notes(
        forecast_count=len(forecasts),
        evaluated_count=len(evaluated),
        settled_trade_count=len(backtest_trades),
    )
    tournament = tournament_context.get(model_name, {})
    if tournament:
        notes = (
            f"{notes} Tournament rank: {tournament.get('overall_rank', 'n/a')}; "
            f"category winner: {'yes' if tournament.get('category_winner') else 'no'}."
        )
    row = {
        "model_name": model_name,
        "generated_at": generated_at,
        "forecast_count": len(forecasts),
        "evaluated_forecast_count": len(evaluated),
        "paper_trade_count": paper_trade_count,
        "settled_trade_count": len(backtest_trades),
        "brier_score": calibration["brier_score"],
        "log_loss": calibration["log_loss"],
        "win_rate": pnl_metrics["win_rate"],
        "total_pnl": pnl_metrics["total_pnl"],
        "roi_on_exposure": pnl_metrics["roi_on_exposure"],
        "avg_edge": pnl_metrics["avg_edge"],
        "max_drawdown": pnl_metrics["max_drawdown"],
        "tournament_rank": tournament.get("overall_rank"),
        "tournament_category": tournament.get("category"),
        "tournament_category_winner": tournament.get("category_winner", False),
        "notes": notes,
    }
    row["raw_json"] = row.copy()
    return row


def _latest_tournament_context(session: Session) -> dict[str, dict[str, Any]]:
    context: dict[str, dict[str, Any]] = {}
    for result in get_latest_tournament_results(session):
        existing = context.get(result.model_name)
        rank = result.overall_rank or 999999
        if existing is None or rank < int(existing.get("overall_rank") or 999999):
            context[result.model_name] = {
                "overall_rank": result.overall_rank,
                "category": result.category,
                "category_winner": (
                    result.overall_rank == 1 and result.status != "INSUFFICIENT_DATA"
                ),
            }
    return context


def _evaluated_forecasts(
    session: Session,
    forecasts: list[Forecast],
) -> list[tuple[Forecast, int]]:
    evaluated: list[tuple[Forecast, int]] = []
    for forecast in forecasts:
        settlement = session.get(Settlement, forecast.ticker)
        y_true = _settlement_to_y_true(settlement.result if settlement else None)
        if y_true is not None:
            evaluated.append((forecast, y_true))
    return evaluated


def _calibration_metrics(evaluated: list[tuple[Forecast, int]]) -> dict[str, Any]:
    if not evaluated:
        return {"brier_score": None, "log_loss": None}
    y_true = [actual for _, actual in evaluated]
    y_prob = [
        float(to_decimal(forecast.yes_probability) or Decimal("0"))
        for forecast, _ in evaluated
    ]
    return {
        "brier_score": brier_score(y_true, y_prob),
        "log_loss": log_loss(y_true, y_prob),
    }


def _backtest_trades(
    session: Session,
    *,
    model_name: str,
    start_time: object,
) -> list[BacktestTrade]:
    return list(
        session.scalars(
            select(BacktestTrade)
            .join(BacktestRun, BacktestTrade.backtest_run_id == BacktestRun.id)
            .where(
                BacktestRun.model_name == model_name,
                BacktestTrade.simulated_at >= start_time,
            )
            .order_by(BacktestTrade.simulated_at, BacktestTrade.id)
        )
    )


def _paper_trade_count(session: Session, model_name: str) -> int:
    value = session.scalar(
        select(func.count()).select_from(PaperOrder).where(PaperOrder.model_name == model_name)
    )
    return int(value or 0)


def _pnl_metrics(trades: list[BacktestTrade]) -> dict[str, Any]:
    if not trades:
        return {
            "win_rate": None,
            "total_pnl": None,
            "roi_on_exposure": None,
            "avg_edge": None,
            "max_drawdown": None,
        }
    total_pnl = sum(((to_decimal(trade.pnl) or Decimal("0")) for trade in trades), Decimal("0"))
    total_edge = sum(((to_decimal(trade.edge) or Decimal("0")) for trade in trades), Decimal("0"))
    exposure = sum(
        (
            ((to_decimal(trade.price) or Decimal("0")) * Decimal(trade.quantity))
            for trade in trades
        ),
        Decimal("0"),
    )
    wins = sum(1 for trade in trades if (to_decimal(trade.pnl) or Decimal("0")) > 0)
    return {
        "win_rate": Decimal(wins) / Decimal(len(trades)),
        "total_pnl": total_pnl,
        "roi_on_exposure": total_pnl / exposure if exposure else Decimal("0"),
        "avg_edge": total_edge / Decimal(len(trades)),
        "max_drawdown": _max_drawdown(trades),
    }


def _max_drawdown(trades: list[BacktestTrade]) -> Decimal:
    peak = Decimal("0")
    cumulative = Decimal("0")
    max_drawdown = Decimal("0")
    for trade in trades:
        cumulative += to_decimal(trade.pnl) or Decimal("0")
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def _settlement_to_y_true(result: str | None) -> int | None:
    if result is None:
        return None
    normalized = result.strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return 1
    if normalized in {"no", "n", "0", "false"}:
        return 0
    return None


def _notes(forecast_count: int, evaluated_count: int, settled_trade_count: int) -> str:
    if forecast_count == 0:
        return "No forecasts found in the selected window."
    if evaluated_count == 0:
        return "Forecasts exist but no matching settlements are available yet."
    if settled_trade_count == 0:
        return "Calibration data exists, but no settled backtest trades are available."
    return "Leaderboard row built from available local calibration and backtest data."


def display_decimal(value: Any) -> str:
    if value is None:
        return "n/a"
    return decimal_to_str(value) or str(value)
