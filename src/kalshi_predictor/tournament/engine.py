from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    BacktestRun,
    BacktestTrade,
    Forecast,
    Market,
    PaperOrder,
    Settlement,
)
from kalshi_predictor.evaluation.metrics import brier_score, log_loss
from kalshi_predictor.tournament.diagnostics import generate_model_diagnostics
from kalshi_predictor.tournament.ranking import (
    TOURNAMENT_MODEL_NAMES,
    assign_status_and_notes,
    classify_forecast_category,
    classify_market_category,
    default_category_for_model,
    rank_tournament_rows,
)
from kalshi_predictor.tournament.repository import (
    complete_tournament_run,
    create_tournament_run,
    insert_model_diagnostic,
    insert_model_weight,
    insert_tournament_result,
)
from kalshi_predictor.tournament.weights import generate_model_weights
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class TournamentResult:
    run_id: int | None
    name: str
    days: int
    rows: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]
    weights: list[dict[str, Any]]


def run_model_tournament(
    session: Session,
    *,
    days: int = 30,
    name: str | None = None,
    generate_weights: bool = True,
    model_names: tuple[str, ...] = TOURNAMENT_MODEL_NAMES,
    persist: bool = True,
) -> TournamentResult:
    started_at = utc_now()
    start_time = started_at - timedelta(days=days)
    tournament_name = name or f"model_tournament_{days}d"
    run = (
        create_tournament_run(
            session,
            name=tournament_name,
            days=days,
            config={
                "days": days,
                "models": list(model_names),
                "generate_weights": generate_weights,
            },
            notes="Model tournament over local stored forecasts and simulated results.",
        )
        if persist
        else None
    )
    run_id = int(run.id) if run is not None and run.id is not None else 0

    rows = _evaluate_rows(
        session,
        model_names=model_names,
        start_time=start_time,
        end_time=started_at,
        tournament_run_id=run_id,
    )
    assign_status_and_notes(rows)
    rank_tournament_rows(rows)
    diagnostics = generate_model_diagnostics(rows)
    weights = generate_model_weights(rows, lookback_days=days) if generate_weights else []

    if persist and run is not None:
        for row in rows:
            insert_tournament_result(session, row)
        for diagnostic in diagnostics:
            insert_model_diagnostic(session, diagnostic)
        for weight in weights:
            insert_model_weight(session, weight)
        complete_tournament_run(
            session,
            run,
            summary={
                "result_rows": len(rows),
                "diagnostics": len(diagnostics),
                "weights": len(weights),
                "insufficient_data_rows": sum(
                    1 for row in rows if row["status"] == "INSUFFICIENT_DATA"
                ),
            },
        )

    return TournamentResult(
        run_id=run.id if run is not None else None,
        name=tournament_name,
        days=days,
        rows=rows,
        diagnostics=diagnostics,
        weights=weights,
    )


def _evaluate_rows(
    session: Session,
    *,
    model_names: tuple[str, ...],
    start_time: Any,
    end_time: Any,
    tournament_run_id: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model_name in model_names:
        forecasts = list(
            session.scalars(
                select(Forecast)
                .where(
                    Forecast.model_name == model_name,
                    Forecast.forecasted_at >= start_time,
                    Forecast.forecasted_at <= end_time,
                )
                .order_by(Forecast.forecasted_at, Forecast.id)
            )
        )
        grouped_forecasts: dict[str, list[Forecast]] = defaultdict(list)
        if forecasts:
            for forecast in forecasts:
                grouped_forecasts[classify_forecast_category(session, forecast)].append(forecast)
        else:
            grouped_forecasts[default_category_for_model(model_name)] = []

        backtest_trades = _backtest_trades(session, model_name, start_time)
        paper_orders = _paper_orders(session, model_name, start_time)
        for category, category_forecasts in grouped_forecasts.items():
            category_trades = [
                trade
                for trade in backtest_trades
                if _category_for_ticker(session, trade.ticker) == category
            ]
            category_orders = [
                order
                for order in paper_orders
                if _category_for_ticker(session, order.ticker) == category
            ]
            rows.append(
                _result_row(
                    session,
                    model_name=model_name,
                    category=category,
                    forecasts=category_forecasts,
                    backtest_trades=category_trades,
                    paper_order_count=len(category_orders),
                    tournament_run_id=tournament_run_id,
                )
            )
    return rows


def _result_row(
    session: Session,
    *,
    model_name: str,
    category: str,
    forecasts: list[Forecast],
    backtest_trades: list[BacktestTrade],
    paper_order_count: int,
    tournament_run_id: int,
) -> dict[str, Any]:
    evaluated = _evaluated_forecasts(session, forecasts)
    calibration = _calibration_metrics(evaluated)
    pnl = _pnl_metrics(backtest_trades)
    row = {
        "tournament_run_id": tournament_run_id,
        "model_name": model_name,
        "category": category,
        "forecast_count": len(forecasts),
        "evaluated_forecast_count": len(evaluated),
        "simulated_trade_count": paper_order_count + len(backtest_trades),
        "settled_trade_count": len(backtest_trades),
        "brier_score": calibration["brier_score"],
        "log_loss": calibration["log_loss"],
        "win_rate": pnl["win_rate"],
        "total_pnl": pnl["total_pnl"],
        "roi_on_exposure": pnl["roi_on_exposure"],
        "avg_edge": pnl["avg_edge"],
        "max_drawdown": pnl["max_drawdown"],
        "calibration_rank": None,
        "pnl_rank": None,
        "overall_rank": None,
        "status": "INSUFFICIENT_DATA",
        "notes": "",
    }
    row["raw_json"] = {
        "model_name": model_name,
        "category": category,
        "forecast_ids": [forecast.id for forecast in forecasts],
        "backtest_trade_ids": [trade.id for trade in backtest_trades],
    }
    return row


def _evaluated_forecasts(session: Session, forecasts: list[Forecast]) -> list[tuple[Forecast, int]]:
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


def _backtest_trades(session: Session, model_name: str, start_time: Any) -> list[BacktestTrade]:
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


def _paper_orders(session: Session, model_name: str, start_time: Any) -> list[PaperOrder]:
    return list(
        session.scalars(
            select(PaperOrder)
            .where(PaperOrder.model_name == model_name, PaperOrder.created_at >= start_time)
            .order_by(PaperOrder.created_at, PaperOrder.id)
        )
    )


def _category_for_ticker(session: Session, ticker: str) -> str:
    market = session.get(Market, ticker)
    if market is None:
        return "unknown"
    text = " ".join(
        str(part or "")
        for part in (
            market.ticker,
            market.title,
            market.subtitle,
            market.series_ticker,
            market.event_ticker,
            market.rules_primary,
            market.rules_secondary,
        )
    )
    return classify_market_category(text)


def _settlement_to_y_true(result: str | None) -> int | None:
    if result is None:
        return None
    normalized = result.strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return 1
    if normalized in {"no", "n", "0", "false"}:
        return 0
    return None
