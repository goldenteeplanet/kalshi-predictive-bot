from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import Forecast, PaperOrder, Settlement
from kalshi_predictor.evaluation.metrics import brier_score, log_loss
from kalshi_predictor.meta.repository import insert_meta_performance, row_to_dict
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

EVALUATED_MODELS = (
    "meta_model_v1",
    "meta_ensemble_v1",
    "ensemble_v2",
    "market_implied_v1",
)


@dataclass(frozen=True)
class MetaEvaluationResult:
    days: int
    rows: dict[str, dict[str, Any]]
    performance: dict[str, Any] | None


def evaluate_meta_model(
    session: Session,
    *,
    days: int = 90,
    persist: bool = True,
) -> MetaEvaluationResult:
    session.flush()
    since = utc_now() - timedelta(days=days)
    rows = {
        model_name: _metrics_for_model(session, model_name=model_name, since=since)
        for model_name in EVALUATED_MODELS
    }
    performance_payload = _performance_payload(rows, days=days)
    performance = (
        row_to_dict(insert_meta_performance(session, performance_payload))
        if persist
        else performance_payload
    )
    return MetaEvaluationResult(days=days, rows=rows, performance=performance)


def _metrics_for_model(
    session: Session,
    *,
    model_name: str,
    since: Any,
) -> dict[str, Any]:
    forecasts = list(
        session.scalars(
            select(Forecast).where(
                Forecast.model_name == model_name,
                Forecast.forecasted_at >= since,
            )
        )
    )
    evaluated: list[tuple[int, float]] = []
    for forecast in forecasts:
        settlement = session.get(Settlement, forecast.ticker)
        actual = _actual(settlement.result if settlement else None)
        probability = to_decimal(forecast.yes_probability)
        if actual is None or probability is None:
            continue
        evaluated.append((actual, float(probability)))
    paper = _paper_roi(session, model_name=model_name, since=since)
    return {
        "forecast_count": len(forecasts),
        "evaluated_count": len(evaluated),
        "brier_score": Decimal(str(brier_score(*_split(evaluated)))) if evaluated else None,
        "log_loss": Decimal(str(log_loss(*_split(evaluated)))) if evaluated else None,
        "roi": paper["roi"],
        "win_rate": paper["win_rate"],
        "settled_trade_count": paper["settled_trade_count"],
        "total_pnl": paper["total_pnl"],
    }


def _performance_payload(rows: dict[str, dict[str, Any]], *, days: int) -> dict[str, Any]:
    meta = rows["meta_model_v1"]
    ensemble = rows["ensemble_v2"]
    market = rows["market_implied_v1"]
    notes = "Meta evaluation uses local stored forecasts and settled paper orders only."
    if not meta["evaluated_count"]:
        notes = "Insufficient data: meta_model_v1 has no settled forecast evaluations yet."
    elif meta["brier_score"] is not None and ensemble["brier_score"] is not None:
        if meta["brier_score"] < ensemble["brier_score"]:
            notes = "meta_model_v1 is currently beating ensemble_v2 by Brier score."
        else:
            notes = "meta_model_v1 is not yet beating ensemble_v2 by Brier score."
    return {
        "lookback_days": days,
        "evaluated_count": meta["evaluated_count"],
        "meta_brier_score": meta["brier_score"],
        "ensemble_brier_score": ensemble["brier_score"],
        "market_implied_brier_score": market["brier_score"],
        "meta_log_loss": meta["log_loss"],
        "ensemble_log_loss": ensemble["log_loss"],
        "market_implied_log_loss": market["log_loss"],
        "meta_roi": meta["roi"],
        "ensemble_roi": ensemble["roi"],
        "market_implied_roi": market["roi"],
        "notes": notes,
        "raw_json": {"model_rows": rows},
    }


def _paper_roi(session: Session, *, model_name: str, since: Any) -> dict[str, Any]:
    rows = list(
        session.execute(
            select(PaperOrder, Settlement)
            .join(Settlement, PaperOrder.ticker == Settlement.ticker, isouter=True)
            .where(PaperOrder.model_name == model_name, PaperOrder.created_at >= since)
        )
    )
    settled = []
    for order, settlement in rows:
        if settlement is None or settlement.result not in {"yes", "no"}:
            continue
        pnl = _paper_pnl(order, settlement)
        exposure = (to_decimal(order.limit_price) or Decimal("0")) * Decimal(order.quantity)
        settled.append({"pnl": pnl, "exposure": exposure, "win": Decimal(int(pnl > 0))})
    if not settled:
        return {
            "roi": None,
            "win_rate": None,
            "settled_trade_count": 0,
            "total_pnl": None,
        }
    total_pnl = sum((row["pnl"] for row in settled), Decimal("0"))
    exposure = sum((row["exposure"] for row in settled), Decimal("0"))
    wins = sum((row["win"] for row in settled), Decimal("0"))
    return {
        "roi": total_pnl / exposure if exposure else None,
        "win_rate": wins / Decimal(len(settled)),
        "settled_trade_count": len(settled),
        "total_pnl": total_pnl,
    }


def _paper_pnl(order: PaperOrder, settlement: Settlement) -> Decimal:
    price = to_decimal(order.limit_price) or Decimal("0")
    quantity = Decimal(order.quantity)
    cost = price * quantity
    if order.side == BUY_YES:
        payout = quantity if settlement.result == "yes" else Decimal("0")
    elif order.side == BUY_NO:
        payout = quantity if settlement.result == "no" else Decimal("0")
    else:
        payout = Decimal("0")
    return payout - cost


def _split(rows: list[tuple[int, float]]) -> tuple[list[int], list[float]]:
    return [row[0] for row in rows], [row[1] for row in rows]


def _actual(result: str | None) -> int | None:
    if result == "yes":
        return 1
    if result == "no":
        return 0
    return None
