from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Forecast, PaperOrder, Settlement
from kalshi_predictor.evaluation.metrics import brier_score, log_loss
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.tournament.ranking import CATEGORIES, classify_market_category
from kalshi_predictor.tournament.repository import insert_model_weight
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

from .repository import insert_model_confidence_score
from .scoring import (
    LABEL_NEEDS_DATA,
    LABEL_UNDERPERFORMING,
    promote_category_leaders,
    score_model_confidence_metrics,
)

CONFIDENCE_MODELS = (
    "market_implied_v1",
    "crypto_v2",
    "weather_v2",
    "economic_v1",
    "ensemble_v1",
    "ensemble_v2",
    "meta_model_v1",
    "meta_ensemble_v1",
)


@dataclass(frozen=True)
class ModelConfidenceResult:
    rows: list[dict[str, Any]]
    weights: list[dict[str, Any]]
    scores_inserted: int
    weights_inserted: int


def run_model_confidence_engine(
    session: Session,
    *,
    settings: Settings | None = None,
    days: int = 30,
    persist: bool = True,
    update_weights: bool = True,
) -> ModelConfidenceResult:
    resolved_settings = settings or get_settings()
    session.flush()
    generated_at = utc_now()
    raw_rows = _raw_confidence_rows(session, generated_at=generated_at, days=days)
    scored_rows = [
        score_model_confidence_metrics(row, settings=resolved_settings) for row in raw_rows
    ]
    promote_category_leaders(scored_rows, settings=resolved_settings)
    weights = generate_confidence_weights(
        scored_rows,
        settings=resolved_settings,
        lookback_days=days,
        generated_at=generated_at,
    )
    scores_inserted = 0
    weights_inserted = 0
    if persist:
        for row in scored_rows:
            insert_model_confidence_score(session, row)
            scores_inserted += 1
        if update_weights:
            for weight in weights:
                insert_model_weight(session, weight)
                weights_inserted += 1
    return ModelConfidenceResult(
        rows=scored_rows,
        weights=weights,
        scores_inserted=scores_inserted,
        weights_inserted=weights_inserted,
    )


def generate_confidence_weights(
    rows: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
    lookback_days: int = 30,
    generated_at: Any | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    generated = generated_at or utc_now()
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_category[str(row.get("category") or "general")].append(row)
    weights: list[dict[str, Any]] = []
    for category in CATEGORIES:
        category_rows = by_category.get(category, [])
        raw_weights: dict[str, Decimal] = {}
        for row in category_rows:
            label = str(row.get("confidence_label") or "")
            model_name = str(row.get("model_name"))
            if label == LABEL_UNDERPERFORMING:
                raw_weights[model_name] = Decimal("0")
            elif label == LABEL_NEEDS_DATA:
                raw_weights[model_name] = resolved_settings.model_confidence_exploration_weight
            else:
                confidence = to_decimal(row.get("confidence_score")) or Decimal("0")
                multiplier = Decimal("1.25") if label == "Leader" else Decimal("1")
                raw_weights[model_name] = confidence * multiplier
        total = sum(raw_weights.values(), Decimal("0"))
        if total <= 0:
            raw_weights = {"market_implied_v1": Decimal("1")}
            total = Decimal("1")
        for model_name, raw_weight in raw_weights.items():
            if raw_weight <= 0:
                continue
            weight = (raw_weight / total).quantize(Decimal("0.0001"))
            weights.append(
                {
                    "generated_at": generated,
                    "model_name": model_name,
                    "category": category,
                    "weight": weight,
                    "method": "model_confidence_v1",
                    "lookback_days": lookback_days,
                    "raw_json": {
                        "source": "model_confidence_scores",
                        "raw_weight": decimal_to_str(raw_weight),
                    },
                }
            )
    return weights


def _raw_confidence_rows(
    session: Session,
    *,
    generated_at: Any,
    days: int,
) -> list[dict[str, Any]]:
    since = generated_at - timedelta(days=days)
    forecast_rows = _forecast_metrics(session, since=since)
    paper_rows = _paper_metrics(session, since=since)
    rows: list[dict[str, Any]] = []
    for category in CATEGORIES:
        for model_name in CONFIDENCE_MODELS:
            key = (model_name, category)
            forecast_metrics = forecast_rows.get(key, {})
            paper_metrics = paper_rows.get(key, {})
            row = {
                "generated_at": generated_at,
                "model_name": model_name,
                "category": category,
                "lookback_days": days,
                "forecast_count": forecast_metrics.get("forecast_count", 0),
                "evaluated_forecast_count": forecast_metrics.get(
                    "evaluated_forecast_count",
                    0,
                ),
                "paper_trade_count": paper_metrics.get("paper_trade_count", 0),
                "settled_trade_count": paper_metrics.get("settled_trade_count", 0),
                "brier_score": forecast_metrics.get("brier_score"),
                "log_loss": forecast_metrics.get("log_loss"),
                "win_rate": paper_metrics.get("win_rate"),
                "roi_on_exposure": paper_metrics.get("roi_on_exposure"),
                "total_pnl": paper_metrics.get("total_pnl"),
                "max_drawdown": paper_metrics.get("max_drawdown"),
                "raw_json": {
                    "forecast_metrics": forecast_metrics,
                    "paper_metrics": paper_metrics,
                },
            }
            rows.append(row)
    return rows


def _forecast_metrics(
    session: Session,
    *,
    since: Any,
) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, list[Any]]] = {}
    rows = session.execute(
        select(Forecast, Settlement)
        .join(Settlement, Forecast.ticker == Settlement.ticker)
        .where(Forecast.forecasted_at >= since)
    ).all()
    for forecast, settlement in rows:
        category = _category_for_forecast(forecast)
        key = (forecast.model_name, category)
        bucket = grouped.setdefault(key, {"actuals": [], "probabilities": []})
        actual = _settlement_actual(settlement)
        probability = to_decimal(forecast.yes_probability)
        if actual is None or probability is None:
            continue
        bucket["actuals"].append(actual)
        bucket["probabilities"].append(float(probability))
    metrics: dict[tuple[str, str], dict[str, Any]] = {}
    for key, bucket in grouped.items():
        actuals = bucket["actuals"]
        probabilities = bucket["probabilities"]
        metrics[key] = {
            "forecast_count": len(probabilities),
            "evaluated_forecast_count": len(probabilities),
            "brier_score": Decimal(str(brier_score(actuals, probabilities))),
            "log_loss": Decimal(str(log_loss(actuals, probabilities))),
        }
    return metrics


def _paper_metrics(
    session: Session,
    *,
    since: Any,
) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Decimal]]] = defaultdict(list)
    all_orders = session.execute(
        select(PaperOrder, Forecast, Settlement)
        .join(Forecast, PaperOrder.forecast_id == Forecast.id, isouter=True)
        .join(Settlement, PaperOrder.ticker == Settlement.ticker, isouter=True)
        .where(PaperOrder.created_at >= since)
    ).all()
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for order, forecast, settlement in all_orders:
        category = _category_for_forecast(forecast) if forecast else "general"
        key = (order.model_name, category)
        counts[key] += 1
        if settlement is None or settlement.result not in {"yes", "no"}:
            continue
        pnl = _paper_order_pnl(order, settlement)
        exposure = (to_decimal(order.limit_price) or Decimal("0")) * Decimal(order.quantity)
        grouped[key].append({"pnl": pnl, "exposure": exposure, "win": Decimal(int(pnl > 0))})
    metrics: dict[tuple[str, str], dict[str, Any]] = {}
    for key, settled_rows in grouped.items():
        total_pnl = sum((row["pnl"] for row in settled_rows), Decimal("0"))
        exposure = sum((row["exposure"] for row in settled_rows), Decimal("0"))
        wins = sum((row["win"] for row in settled_rows), Decimal("0"))
        metrics[key] = {
            "paper_trade_count": counts[key],
            "settled_trade_count": len(settled_rows),
            "win_rate": wins / Decimal(len(settled_rows)) if settled_rows else None,
            "roi_on_exposure": total_pnl / exposure if exposure > 0 else None,
            "total_pnl": total_pnl,
            "max_drawdown": _max_drawdown([row["pnl"] for row in settled_rows]),
        }
    for key, count in counts.items():
        metrics.setdefault(
            key,
            {
                "paper_trade_count": count,
                "settled_trade_count": 0,
                "win_rate": None,
                "roi_on_exposure": None,
                "total_pnl": None,
                "max_drawdown": None,
            },
        )
    return metrics


def _category_for_forecast(forecast: Forecast) -> str:
    text = f"{forecast.ticker} {forecast.notes or ''}"
    return classify_market_category(text)


def _settlement_actual(settlement: Settlement) -> int | None:
    if settlement.result == "yes":
        return 1
    if settlement.result == "no":
        return 0
    return None


def _paper_order_pnl(order: PaperOrder, settlement: Settlement) -> Decimal:
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


def _max_drawdown(pnls: list[Decimal]) -> Decimal | None:
    if not pnls:
        return None
    cumulative = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return abs(max_drawdown)
