from __future__ import annotations

import argparse
import copy
import json
from datetime import UTC, datetime
from decimal import ROUND_FLOOR
from pathlib import Path

from sqlalchemy import desc, func, select

from kalshi_predictor.config import get_settings
from kalshi_predictor.data.backend import database_url_from_settings
from kalshi_predictor.data.db import get_session_factory, make_engine
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import (
    Forecast,
    MarketRanking,
    PaperOrder,
    PositionSizingDecisionLog,
)
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.kalshi.orderbook import usable_bid_ask_book
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.paper.models import PaperDecision
from kalshi_predictor.position_sizing.service import (
    ensure_paper_decision_sized,
    prepare_position_sizing_historical_evidence,
)
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def evaluate_paper_decision_without_persisting(session, decision, *, settings):
    """Run Phase 3M/3N for telemetry while rolling back their decision logs."""
    savepoint = session.begin_nested()
    try:
        sized = ensure_paper_decision_sized(session, decision, settings=settings)
        session.flush()
        return copy.deepcopy(sized.raw_decision_json)
    finally:
        savepoint.rollback()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--ticker", action="append", required=True)
    args = parser.parse_args()

    tickers = set(args.ticker)
    payload = json.loads(args.gate.read_text(encoding="utf-8"))
    rows = {
        row["ticker"]: row
        for row in payload.get("weather_rows", payload.get("rows", []))
        if row.get("ticker") in tickers
    }
    if set(rows) != tickers:
        raise RuntimeError(f"Missing scoped gate rows: {sorted(tickers - set(rows))}")
    for ticker, row in rows.items():
        if not all(
            (
                row.get("executable_book") is True,
                row.get("buy_price_matches_ranking") is True,
                row.get("sufficient_buy_side_size") is True,
                row.get("first_blocker")
                in {
                    "PHASE_3M_ZERO_SIZE",
                    "PHASE_3N_RISK_BLOCK",
                    "SNAPSHOT_STALE",
                    "FORECAST_MISSING",
                    "PAPER_READY",
                },
            )
        ):
            raise RuntimeError(f"{ticker} is no longer eligible for scoped sizing")

    base = get_settings()
    settings = learning_paper_settings(base).model_copy(
        update={
            "execution_enabled": False,
            "execution_dry_run": True,
            "paper_order_creation_enabled": False,
            "paper_order_kill_switch": True,
            "dynamic_position_sizing_mode": "shadow",
            "advanced_risk_engine_mode": "shadow",
        }
    )
    engine = make_engine(database_url_from_settings(base))
    if args.prepare_only:
        if args.cache.exists():
            existing_cache = json.loads(args.cache.read_text(encoding="utf-8"))
            if _cache_covers_rows(existing_cache, rows):
                print(json.dumps({"status": "REUSED", **existing_cache}, indent=2, sort_keys=True))
                engine.dispose()
                return
        try:
            with get_session_factory(engine)() as session:
                forecasts = [
                    session.get(Forecast, int(rows[ticker]["forecast_id"]))
                    for ticker in sorted(tickers)
                ]
                if any(forecast is None for forecast in forecasts):
                    raise RuntimeError("A scoped forecast disappeared before cache preparation")
                cache = prepare_position_sizing_historical_evidence(
                    session,
                    forecasts=forecasts,
                )
        finally:
            engine.dispose()
        _write_atomic(args.cache, cache)
        print(json.dumps(cache, indent=2, sort_keys=True))
        return

    cache = json.loads(args.cache.read_text(encoding="utf-8"))
    state = _read_state(args.state)
    results: list[dict] = []
    client = KalshiClient()
    try:
        with get_session_factory(engine)() as session:
            orders_before = int(session.scalar(select(func.count()).select_from(PaperOrder)) or 0)
            for ticker in sorted(tickers):
                row = rows[ticker]
                market_payload = client.get_market(ticker)
                orderbook_payload = client.get_orderbook(ticker)
                captured_at = utc_now()
                snapshot = insert_market_snapshot(
                    session,
                    market_payload,
                    orderbook_payload,
                    captured_at=captured_at,
                )
                run_forecast_models(session, model_name="weather_v2", snapshots=[snapshot])
                session.flush()
                scan_opportunities(
                    session,
                    model_name="weather_v2",
                    limit=1,
                    ticker_scope={ticker},
                    scan_mode="SCOPED_FRESH_WEATHER_RISK_PREFLIGHT",
                )
                session.flush()
                forecast = session.scalar(
                    select(Forecast)
                    .where(
                        Forecast.ticker == ticker,
                        Forecast.model_name == "weather_v2",
                        Forecast.forecasted_at >= captured_at,
                    )
                    .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
                    .limit(1)
                )
                ranking = session.scalar(
                    select(MarketRanking)
                    .where(
                        MarketRanking.ticker == ticker,
                        MarketRanking.forecast_model == "weather_v2",
                        MarketRanking.ranked_at >= captured_at,
                    )
                    .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
                    .limit(1)
                )
                if ranking is None or forecast is None:
                    raise RuntimeError(f"{ticker}: fresh forecast or ranking was not generated")
                cache_for_forecast = _cache_for_fresh_forecast(cache, forecast)
                pair_key = f"forecast:{forecast.id}:snapshot:{snapshot.id}"
                existing_pair = session.scalar(
                    select(PositionSizingDecisionLog)
                    .where(PositionSizingDecisionLog.raw_json.contains(pair_key))
                    .order_by(desc(PositionSizingDecisionLog.id))
                    .limit(1)
                )
                if existing_pair is not None or pair_key in state["completed_pair_keys"]:
                    results.append(
                        {
                            "ticker": ticker,
                            "forecast_snapshot_pair_key": pair_key,
                            "status": "IDEMPOTENT_SKIP",
                            "phase3m_decision_id": existing_pair.id if existing_pair else None,
                        }
                    )
                    continue
                book = usable_bid_ask_book(
                    orderbook_payload,
                    side=ranking.best_side,
                    liquidity_score=ranking.liquidity_score,
                    min_liquidity_score=to_decimal("25"),
                    max_spread=settings.opportunity_max_spread,
                )
                price = to_decimal(ranking.best_price)
                if not book.usable or book.ask_price != price:
                    raise RuntimeError(
                        f"{ticker}: fresh executable ask {book.ask_price} does not match ranking {price}"
                    )
                depth = book.ask_depth
                depth_cap = int(depth.to_integral_value(rounding=ROUND_FLOOR)) if depth else 0
                if depth_cap < 1:
                    raise RuntimeError(f"{ticker}: exact-level depth cap is zero")
                raw = evaluate_paper_decision_without_persisting(
                    session,
                    PaperDecision(
                        ticker=ticker,
                        forecast_id=forecast.id,
                        model_name="weather_v2",
                        side=ranking.best_side,
                        probability=to_decimal(ranking.forecast_probability),
                        market_price=price,
                        limit_price=price,
                        edge=to_decimal(ranking.estimated_edge),
                        quantity=min(settings.paper_max_order_quantity, depth_cap),
                        reason=(
                            "Scoped paper-only weather Phase 3M/3N preflight; "
                            "exact-level depth capped; no order creation."
                        ),
                        raw_decision_json={
                            "source": "phase3ba_r3_scoped_weather_depth_preflight_v1",
                            "strategy": "weather_v2_scoped_depth_preflight",
                            "risk_preflight_only": True,
                            "execution_enabled": False,
                            "paper_order_creation_enabled": False,
                            "ranking_id": ranking.id,
                            "forecast_id": forecast.id,
                            "snapshot_id": snapshot.id,
                            "snapshot_at": captured_at.isoformat(),
                            "actual_buy_side": row["actual_buy_side"],
                            "actual_buy_price": row["derived_executable_buy_price"],
                            "actual_buy_price_source": row["actual_buy_price_source"],
                            "exact_level_depth": str(depth),
                            "exact_level_depth_cap_contracts": depth_cap,
                            "buy_price_matches_ranking": True,
                            "forecast_snapshot_pair_key": pair_key,
                            "position_sizing_historical_evidence_cache": cache_for_forecast,
                        },
                    ),
                    settings=settings,
                )
                sizing = raw["position_sizing_decision"]
                risk = raw["advanced_risk_decision"]
                proposed = int(sizing["proposed_contracts"])
                if proposed > depth_cap:
                    raise RuntimeError(
                        f"{ticker}: Phase 3M proposal {proposed} exceeds depth cap {depth_cap}"
                    )
                results.append(
                    {
                        "ticker": ticker,
                        "status": "RECORDED",
                        "forecast_snapshot_pair_key": pair_key,
                        "buy_price": str(price),
                        "exact_level_depth": str(depth),
                        "fresh_snapshot_id": snapshot.id,
                        "fresh_snapshot_at": captured_at.isoformat(),
                        "depth_cap_contracts": depth_cap,
                        "phase3m_decision_id": None,
                        "phase3m_evaluation_id": raw["position_sizing_decision_id"],
                        "phase3m_tier": sizing["tier"],
                        "phase3m_proposed_contracts": proposed,
                        "phase3m_live_candidate_contracts": sizing["live_candidate_contracts"],
                        "phase3m_limiting_factors": sizing["limiting_factors"],
                        "phase3n_decision_id": None,
                        "phase3n_evaluation_id": raw["advanced_risk_decision_id"],
                        "phase3n_action": risk["action"],
                        "phase3n_live_candidate_contracts": risk["live_candidate_contracts"],
                        "phase3n_hard_blocks": risk["hard_blocks"],
                        "phase3n_reason_codes": risk["reason_codes"],
                    }
                )
            orders_after = int(session.scalar(select(func.count()).select_from(PaperOrder)) or 0)
            if orders_after != orders_before:
                raise RuntimeError(f"Paper order count changed: {orders_before} -> {orders_after}")
            session.commit()
    finally:
        client.close()
        engine.dispose()

    output = {
        "generated_at": utc_now().isoformat(),
        "mode": "PAPER_READ_ONLY_PREFLIGHT",
        "paper_orders_before": orders_before,
        "paper_orders_after": orders_after,
        "results": results,
    }
    _write_atomic(args.output, output)
    if args.state is not None:
        state["completed_pair_keys"] = list(
            dict.fromkeys(
                [*state["completed_pair_keys"]]
                + [
                    row["forecast_snapshot_pair_key"]
                    for row in results
                    if row.get("status") == "RECORDED"
                ]
            )
        )[-500:]
        state["updated_at"] = utc_now().isoformat()
        _write_atomic(args.state, state)
    print(json.dumps(output, indent=2, sort_keys=True))


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _cache_for_fresh_forecast(cache: dict, forecast: Forecast) -> dict:
    entries = cache.get("entries") or {}
    source = next(
        (
            entry
            for entry in entries.values()
            if entry.get("ticker") == forecast.ticker
            and entry.get("model_name") == forecast.model_name
        ),
        None,
    )
    if source is None:
        raise RuntimeError(f"No prepared history evidence for fresh forecast {forecast.ticker}")
    fresh_entry = {
        **source,
        "forecast_id": forecast.id,
        "ticker": forecast.ticker,
        "model_name": forecast.model_name,
    }
    return {**cache, "entries": {str(forecast.id): fresh_entry}}


def _cache_covers_rows(cache: dict, rows: dict[str, dict]) -> bool:
    if cache.get("version") != "phase3m_historical_evidence_v1":
        return False
    try:
        prepared = datetime.fromisoformat(str(cache["prepared_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    if prepared.tzinfo is None:
        prepared = prepared.replace(tzinfo=UTC)
    if (datetime.now(UTC) - prepared).total_seconds() > 6 * 60 * 60:
        return False
    cached_tickers = {
        entry.get("ticker")
        for entry in (cache.get("entries") or {}).values()
        if entry.get("model_name") == "weather_v2"
    }
    return set(rows).issubset(cached_tickers)


def _read_state(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {"version": "weather_preflight_pair_state_v1", "completed_pair_keys": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    keys = payload.get("completed_pair_keys")
    payload["completed_pair_keys"] = keys if isinstance(keys, list) else []
    return payload


if __name__ == "__main__":
    main()
