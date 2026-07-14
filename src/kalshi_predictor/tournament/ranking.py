import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Forecast, Market
from kalshi_predictor.utils.decimals import to_decimal

TOURNAMENT_MODEL_NAMES = (
    "market_implied_v1",
    "crypto_v2",
    "weather_v2",
    "economic_v1",
    "news_v1",
    "mlb_v1",
    "nba_v1",
    "nfl_v1",
    "nhl_v1",
    "sports_v1",
    "microstructure_v1",
    "meta_model_v1",
    "meta_ensemble_v1",
    "ensemble_v1",
    "ensemble_v2",
)

CATEGORIES = ("crypto", "weather", "economic", "sports", "general", "unknown")
SUFFICIENT_EVALUATED_FORECASTS = 5


def classify_forecast_category(session: Session, forecast: Forecast) -> str:
    market = session.get(Market, forecast.ticker)
    if market is not None:
        category = classify_market_category(_market_text(market))
        if category != "unknown":
            return category
    return default_category_for_model(forecast.model_name)


def classify_market_category(text: str) -> str:
    normalized = text.lower()
    if re.search(r"\b(btc|bitcoin|eth|ethereum|crypto(?:currency)?)\b", normalized):
        return "crypto"
    if re.search(
        r"\b(weather|temperature|temp|rain|snow|wind|hurricane|freeze|precipitation)\b",
        normalized,
    ):
        return "weather"
    if re.search(r"\b(cpi|inflation|fed|interest rate|gdp|jobs?|unemployment)\b", normalized):
        return "economic"
    if re.search(r"\b(nfl|nba|mlb|nhl|soccer|football|basketball|baseball|team)\b", normalized):
        return "sports"
    if normalized.strip():
        return "general"
    return "unknown"


def default_category_for_model(model_name: str) -> str:
    if "crypto" in model_name:
        return "crypto"
    if "weather" in model_name:
        return "weather"
    if "economic" in model_name:
        return "economic"
    if "sport" in model_name or model_name in {"mlb_v1", "nba_v1", "nfl_v1", "nhl_v1"}:
        return "sports"
    if "microstructure" in model_name:
        return "general"
    return "general"


def assign_status_and_notes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        if int(row.get("evaluated_forecast_count") or 0) < SUFFICIENT_EVALUATED_FORECASTS:
            row["status"] = "INSUFFICIENT_DATA"
            row["notes"] = "Not enough settled forecasts for reliable tournament ranking."
        else:
            row["status"] = "OK"
            row["notes"] = "Sufficient settled forecasts for ranking."
    return rows


def rank_tournament_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_category[str(row["category"])].append(row)

    for category_rows in by_category.values():
        _assign_rank(
            category_rows,
            key=lambda row: (
                _missing_last(row.get("brier_score")),
                _missing_last(row.get("log_loss")),
            ),
            field="calibration_rank",
            reverse=False,
        )
        _assign_rank(
            category_rows,
            key=lambda row: (
                _missing_first(row.get("roi_on_exposure")),
                _missing_first(row.get("total_pnl")),
            ),
            field="pnl_rank",
            reverse=True,
        )
        _assign_rank(
            category_rows,
            key=overall_score,
            field="overall_rank",
            reverse=True,
        )
    return rows


def overall_score(row: dict[str, Any]) -> Decimal:
    brier = to_decimal(row.get("brier_score"))
    logloss = to_decimal(row.get("log_loss"))
    roi = to_decimal(row.get("roi_on_exposure")) or Decimal("0")
    pnl = to_decimal(row.get("total_pnl")) or Decimal("0")
    max_drawdown = abs(to_decimal(row.get("max_drawdown")) or Decimal("0"))
    evaluated = Decimal(int(row.get("evaluated_forecast_count") or 0))

    if brier is None and logloss is None:
        calibration_score = Decimal("0")
    else:
        calibration_score = Decimal("1") / (
            Decimal("1") + (brier or Decimal("0")) + (logloss or Decimal("0"))
        )
    pnl_score = roi + (pnl / Decimal("100"))
    sample_score = min(evaluated / Decimal("100"), Decimal("1"))
    drawdown_penalty = min(max_drawdown / (abs(pnl) + Decimal("1")), Decimal("1"))
    return (
        calibration_score * Decimal("0.40")
        + pnl_score * Decimal("0.35")
        + sample_score * Decimal("0.15")
        - drawdown_penalty * Decimal("0.10")
    )


def _assign_rank(
    rows: list[dict[str, Any]],
    *,
    key: Any,
    field: str,
    reverse: bool,
) -> None:
    for index, row in enumerate(sorted(rows, key=key, reverse=reverse), start=1):
        row[field] = index


def _missing_last(value: Any) -> Decimal:
    parsed = to_decimal(value)
    if parsed is None:
        return Decimal("999999")
    return parsed


def _missing_first(value: Any) -> Decimal:
    parsed = to_decimal(value)
    if parsed is None:
        return Decimal("-999999")
    return parsed


def _market_text(market: Market) -> str:
    raw = decode_json(market.raw_json)
    return " ".join(
        str(part or "")
        for part in (
            market.ticker,
            market.title,
            market.subtitle,
            market.series_ticker,
            market.event_ticker,
            market.rules_primary,
            market.rules_secondary,
            raw.get("rules"),
        )
    )
