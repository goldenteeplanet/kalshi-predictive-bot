from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import httpx

from kalshi_predictor.config import Settings
from kalshi_predictor.kalshi.orderbook import LocalOrderbook
from kalshi_predictor.opportunities.payout_scoring import calculate_payout_metrics
from kalshi_predictor.opportunities.scoring import (
    score_liquidity,
    score_model_confidence,
    score_spread,
    score_time_to_close,
)
from kalshi_predictor.phase_gh1h import PRODUCTION_PUBLIC_REST_URL
from kalshi_predictor.phase_gh1k import depth_notional
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

MIN_EXECUTABLE_LIQUIDITY_SCORE = Decimal("30")


def evaluate_independent_forecast(*, forecast: Mapping[str, Any], market: Mapping[str, Any],
                                  orderbook: Mapping[str, Any], settings: Settings,
                                  max_forecast_age_minutes: Decimal) -> dict[str, Any]:
    now = utc_now()
    forecasted_at = parse_datetime(forecast.get("forecasted_at"))
    age = Decimal(str((now - forecasted_at).total_seconds() / 60)) if forecasted_at else None
    ticker = str(forecast["ticker"])
    book = LocalOrderbook(ticker)
    book.apply_rest_snapshot(orderbook, resume_sequence=0)
    probability = to_decimal(forecast.get("yes_probability"))
    yes_edge = probability - book.best_yes_ask if probability is not None and book.best_yes_ask is not None else None
    no_probability = Decimal("1") - probability if probability is not None else None
    no_edge = no_probability - book.best_no_ask if no_probability is not None and book.best_no_ask is not None else None
    choices = [("BUY_YES", book.best_yes_ask, yes_edge), ("BUY_NO", book.best_no_ask, no_edge)]
    usable = [choice for choice in choices if choice[1] is not None and choice[2] is not None]
    side, price, edge = max(usable, key=lambda item: item[2]) if usable else (None, None, None)
    top5_notional = depth_notional(book)
    liquidity_score = score_liquidity(
        volume=market.get("volume_fp"), open_interest=market.get("open_interest_fp"),
        liquidity=max(to_decimal(market.get("liquidity_dollars")) or Decimal("0"), top5_notional),
    )
    spread_score = score_spread(book.spread, max_spread=settings.opportunity_max_spread)
    confidence_score = score_model_confidence(probability)
    close_time = parse_datetime(market.get("close_time"))
    time_minutes = Decimal(str((close_time - now).total_seconds() / 60)) if close_time else None
    time_score = score_time_to_close(time_minutes, min_minutes=settings.opportunity_min_time_to_close_minutes)
    metrics = calculate_payout_metrics(
        side=side, yes_probability=probability, cost=price, edge=edge,
        liquidity_score=liquidity_score, spread_score=spread_score,
        confidence_score=confidence_score, time_score=time_score,
    )
    blockers: list[str] = []
    if age is None or age > max_forecast_age_minutes:
        blockers.append("FORECAST_STALE")
    if edge is None or edge < settings.opportunity_min_edge:
        blockers.append("EDGE_BELOW_MINIMUM")
    if metrics.payout_adjusted_score < settings.opportunity_min_score:
        blockers.append("OPPORTUNITY_SCORE_BELOW_MINIMUM")
    if book.spread is None or book.spread > settings.opportunity_max_spread:
        blockers.append("SPREAD_NOT_EXECUTABLE")
    if book.depth(side="yes", levels=5) < 1 or book.depth(side="no", levels=5) < 1:
        blockers.append("TWO_SIDED_DEPTH_MISSING")
    if liquidity_score < MIN_EXECUTABLE_LIQUIDITY_SCORE:
        blockers.append("LIQUIDITY_SCORE_BELOW_EXECUTABLE")
    if time_minutes is not None and time_minutes < settings.opportunity_min_time_to_close_minutes:
        blockers.append("TIME_TO_CLOSE_BELOW_MINIMUM")
    return {
        "ticker": ticker, "model_name": forecast.get("model_name"),
        "forecasted_at": forecast.get("forecasted_at"), "forecast_age_minutes": _string(age),
        "yes_probability": _string(probability), "best_side": side, "best_price": _string(price),
        "executable_edge": _string(edge), "expected_value": _string(metrics.expected_value),
        "opportunity_score": str(metrics.payout_adjusted_score), "spread": _string(book.spread),
        "liquidity_score": str(liquidity_score), "top5_notional": str(top5_notional),
        "time_to_close_minutes": _string(time_minutes), "blockers": blockers,
        "first_blocker": blockers[0] if blockers else None,
        "advance": not blockers,
    }


def write_gh1o_report(*, database_path: Path, settings: Settings, output_dir: Path,
                      models: list[str], max_forecasts: int, max_forecast_age_minutes: Decimal,
                      rest_base_url: str = PRODUCTION_PUBLIC_REST_URL) -> Path:
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    forecasts = []
    for model in models:
        forecasts.extend(connection.execute(
            "SELECT f.* FROM forecasts f JOIN (SELECT ticker, MAX(forecasted_at) latest "
            "FROM forecasts WHERE model_name=? GROUP BY ticker) x "
            "ON f.ticker=x.ticker AND f.forecasted_at=x.latest "
            "WHERE f.model_name=? ORDER BY f.forecasted_at DESC LIMIT ?",
            (model, model, max_forecasts),
        ).fetchall())
    rows: list[dict[str, Any]] = []
    skips: dict[str, int] = {}
    try:
        with httpx.Client(base_url=rest_base_url, timeout=15.0) as client:
            for stored in forecasts:
                ticker = stored["ticker"]
                market_response = client.get(f"/markets/{ticker}")
                if market_response.status_code != 200:
                    skips["PUBLIC_MARKET_UNAVAILABLE"] = skips.get("PUBLIC_MARKET_UNAVAILABLE", 0) + 1
                    continue
                market = market_response.json().get("market", market_response.json())
                if str(market.get("status") or "").lower() not in {"open", "active"}:
                    skips["MARKET_NOT_ACTIVE"] = skips.get("MARKET_NOT_ACTIVE", 0) + 1
                    continue
                book_response = client.get(f"/markets/{ticker}/orderbook", params={"depth": 5})
                if book_response.status_code != 200:
                    skips["ORDERBOOK_UNAVAILABLE"] = skips.get("ORDERBOOK_UNAVAILABLE", 0) + 1
                    continue
                rows.append(evaluate_independent_forecast(
                    forecast=dict(stored), market=market, orderbook=book_response.json(),
                    settings=settings, max_forecast_age_minutes=max_forecast_age_minutes,
                ))
    finally:
        connection.close()
    report = {
        "phase": "GH-1O", "generated_at": utc_now().isoformat(),
        "mode": "INDEPENDENT_FORECAST_PUBLIC_BOOK_READ_ONLY", "execution_enabled": False,
        "database_writes": 0, "thresholds_changed": False, "models": models,
        "evaluations": rows,
        "summary": {"stored_forecasts_scanned": len(forecasts), "live_markets_evaluated": len(rows),
                    "positive_executable_edge": sum((to_decimal(row["executable_edge"]) or Decimal("0")) > 0 for row in rows),
                    "advanced_candidates": sum(row["advance"] for row in rows),
                    "first_blocker_counts": _counts(row["first_blocker"] for row in rows),
                    "skip_counts": skips},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1o_independent_model_executable_edge_discovery.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _counts(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        key = str(value or "NONE")
        result[key] = result.get(key, 0) + 1
    return result


def _string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
