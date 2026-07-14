from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.metrics import calculate_backtest_metrics
from kalshi_predictor.backtesting.strategy import BacktestDecision, paper_decision_for_backtest
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    BacktestRun,
    BacktestTrade,
    Forecast,
    MarketSnapshot,
    Settlement,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class BacktestResult:
    run_id: int | None
    model_name: str
    strategy_name: str
    forecasts_scanned: int
    evaluated_forecasts: int
    trades: list[dict[str, Any]]
    summary: dict[str, Any]


def run_backtest(
    session: Session,
    *,
    model_name: str,
    strategy_name: str = "paper_v1",
    days: int = 30,
    settings: Settings | None = None,
    persist: bool = True,
    name: str | None = None,
) -> BacktestResult:
    if strategy_name != "paper_v1":
        raise ValueError(f"Unsupported backtest strategy: {strategy_name}")

    session.flush()
    resolved_settings = settings or get_settings()
    end_time = utc_now()
    start_time = end_time - timedelta(days=days)
    run: BacktestRun | None = None
    if persist:
        run = BacktestRun(
            name=name or f"{strategy_name}:{model_name}:{days}d",
            strategy_name=strategy_name,
            model_name=model_name,
            started_at=end_time,
            completed_at=None,
            start_time=start_time,
            end_time=end_time,
            config_json=encode_json(
                {
                    "days": days,
                    "paper_min_edge": str(resolved_settings.paper_min_edge),
                    "paper_max_order_quantity": resolved_settings.paper_max_order_quantity,
                    "paper_default_fee_per_contract": str(
                        resolved_settings.paper_default_fee_per_contract
                    ),
                }
            ),
            summary_json=None,
            notes="Historical simulated paper_v1 backtest over local stored data.",
        )
        session.add(run)
        session.flush()

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
    trades: list[dict[str, Any]] = []
    seen_forecast_ids: set[int] = set()
    evaluated_forecasts = 0

    for forecast in forecasts:
        if forecast.id is None or forecast.id in seen_forecast_ids:
            continue
        settlement = session.get(Settlement, forecast.ticker)
        y_true = _settlement_to_y_true(settlement.result if settlement else None)
        if y_true is None:
            continue
        evaluated_forecasts += 1
        snapshot = _snapshot_for_forecast(session, forecast)
        decision = paper_decision_for_backtest(forecast, snapshot, resolved_settings)
        if decision is None:
            continue
        seen_forecast_ids.add(forecast.id)
        trade = _trade_from_decision(
            decision,
            y_true=y_true,
            settlement_result=settlement.result if settlement else None,
            fee_per_contract=resolved_settings.paper_default_fee_per_contract,
        )
        trades.append(trade)
        if persist and run is not None:
            session.add(
                BacktestTrade(
                    backtest_run_id=run.id,
                    ticker=decision.ticker,
                    forecast_id=decision.forecast_id,
                    simulated_at=forecast.forecasted_at,
                    side=decision.side,
                    price=decimal_to_str(decision.price) or "0",
                    quantity=decision.quantity,
                    edge=decimal_to_str(decision.edge) or "0",
                    settlement_result=settlement.result if settlement else None,
                    pnl=trade["pnl"],
                    raw_decision_json=encode_json(decision.raw_decision_json),
                )
            )

    summary = calculate_backtest_metrics(trades)
    summary["forecasts_scanned"] = len(forecasts)
    summary["evaluated_forecasts"] = evaluated_forecasts
    if persist and run is not None:
        run.completed_at = utc_now()
        run.summary_json = encode_json(summary)
        session.add(run)

    return BacktestResult(
        run_id=run.id if run is not None else None,
        model_name=model_name,
        strategy_name=strategy_name,
        forecasts_scanned=len(forecasts),
        evaluated_forecasts=evaluated_forecasts,
        trades=trades,
        summary=summary,
    )


def _snapshot_for_forecast(session: Session, forecast: Forecast) -> MarketSnapshot | None:
    snapshot = session.scalar(
        select(MarketSnapshot)
        .where(
            MarketSnapshot.ticker == forecast.ticker,
            MarketSnapshot.captured_at <= forecast.forecasted_at,
        )
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )
    if snapshot is not None:
        return snapshot
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == forecast.ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _trade_from_decision(
    decision: BacktestDecision,
    *,
    y_true: int,
    settlement_result: str | None,
    fee_per_contract: Decimal,
) -> dict[str, Any]:
    fee = fee_per_contract * Decimal(decision.quantity)
    cost = decision.price * decision.quantity
    exposure = cost + fee
    if decision.side == BUY_YES:
        payout = Decimal(decision.quantity) if y_true == 1 else Decimal("0")
    elif decision.side == BUY_NO:
        payout = Decimal(decision.quantity) if y_true == 0 else Decimal("0")
    else:
        payout = Decimal("0")
    pnl = payout - exposure
    return {
        "ticker": decision.ticker,
        "forecast_id": decision.forecast_id,
        "simulated_at": decision.simulated_at.isoformat(),
        "side": decision.side,
        "price": decimal_to_str(decision.price) or "0",
        "quantity": decision.quantity,
        "edge": decimal_to_str(decision.edge) or "0",
        "settlement_result": settlement_result,
        "pnl": decimal_to_str(pnl) or "0",
        "exposure": decimal_to_str(exposure) or "0",
        "yes_probability": float(decision.yes_probability),
        "y_true": y_true,
    }


def _settlement_to_y_true(result: str | None) -> int | None:
    if result is None:
        return None
    normalized = result.strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return 1
    if normalized in {"no", "n", "0", "false"}:
        return 0
    return None
