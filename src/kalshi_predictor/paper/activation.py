from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import (
    Forecast,
    MarketRanking,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
)
from kalshi_predictor.paper.ledger import create_paper_order
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, PaperDecision
from kalshi_predictor.paper.simulator import simulate_immediate_fill
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

ACTIVATION_VERSION = "weather_one_contract_paper_activation_v1"
PAIR_PATTERN = re.compile(r"^forecast:(\d+):snapshot:(\d+)$")


@dataclass(frozen=True)
class PaperActivationCandidate:
    ticker: str
    forecast: Forecast
    snapshot: MarketSnapshot
    ranking: MarketRanking
    gate_row: dict[str, Any]
    preflight_row: dict[str, Any]
    pair_key: str


def required_approval_phrase(ticker: str) -> str:
    return f"AUTHORIZE ONE PAPER CONTRACT {ticker}"


def validate_one_contract_paper_activation(
    session: Session,
    *,
    ticker: str,
    soak_path: Path,
    gate_path: Path,
    preflight_path: Path,
    pair_state_path: Path,
    settings: Settings,
    now: datetime | None = None,
) -> PaperActivationCandidate:
    current = now or utc_now()
    soak = _read_json(soak_path)
    gate = _read_json(gate_path)
    preflight = _read_json(preflight_path)
    pair_state = _read_json(pair_state_path)
    if not soak.get("soak_complete") or int(soak.get("consecutive_healthy_cycles") or 0) < 3:
        raise RuntimeError("Three consecutive healthy paper-only soak cycles are required.")
    if not soak.get("explicit_operator_approval_required"):
        raise RuntimeError("Soak artifact does not retain explicit operator approval.")
    if soak.get("paper_order_creation_enabled") is not False:
        raise RuntimeError("Soak artifact must prove paper-order creation remained disabled.")

    gate_row = _ticker_row(gate, ticker, "weather_rows")
    if gate_row.get("paper_ready") is not True or gate_row.get("first_blocker") != "PAPER_READY":
        raise RuntimeError(f"{ticker} is not currently PAPER_READY.")
    preflight_row = _ticker_row(preflight, ticker, "results")
    if preflight_row.get("status") != "RECORDED":
        raise RuntimeError("Latest coherent preflight was not recorded.")
    if preflight_row.get("phase3n_action") != "ALLOW" or preflight_row.get(
        "phase3n_hard_blocks"
    ):
        raise RuntimeError("Latest Phase 3N decision is not an unblocked ALLOW.")

    pair_key = str(preflight_row.get("forecast_snapshot_pair_key") or "")
    match = PAIR_PATTERN.fullmatch(pair_key)
    if match is None:
        raise RuntimeError("Preflight lacks an exact forecast/snapshot pair key.")
    forecast_id, snapshot_id = (int(value) for value in match.groups())
    if pair_key not in set(pair_state.get("completed_pair_keys") or []):
        raise RuntimeError("Forecast/snapshot pair is absent from retained idempotency state.")
    forecast = session.get(Forecast, forecast_id)
    snapshot = session.get(MarketSnapshot, snapshot_id)
    if forecast is None or snapshot is None or forecast.ticker != ticker or snapshot.ticker != ticker:
        raise RuntimeError("Forecast/snapshot pair does not resolve to the requested ticker.")
    ranking = session.scalar(
        select(MarketRanking)
        .where(
            MarketRanking.ticker == ticker,
            MarketRanking.forecast_model == "weather_v2",
            MarketRanking.ranked_at >= forecast.forecasted_at,
        )
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(1)
    )
    if ranking is None or int(gate_row.get("ranking_id") or 0) != ranking.id:
        raise RuntimeError("Current gate ranking does not match the coherent forecast.")
    if int(gate_row.get("forecast_id") or 0) != forecast.id:
        raise RuntimeError("Current gate forecast does not match the coherent pair.")
    captured_at = _aware(snapshot.captured_at)
    forecasted_at = _aware(forecast.forecasted_at)
    if forecasted_at < captured_at:
        raise RuntimeError("Forecast predates its exact market snapshot.")
    quote_age_ms = int((_aware(current) - captured_at).total_seconds() * 1000)
    if quote_age_ms < 0 or quote_age_ms > settings.advanced_risk_quote_max_age_ms:
        raise RuntimeError("Exact forecast/snapshot pair is stale.")
    if to_decimal(ranking.best_price) != to_decimal(preflight_row.get("buy_price")):
        raise RuntimeError("Ranking price no longer matches the coherent executable BUY price.")
    if int(preflight_row.get("phase3m_proposed_contracts") or 0) != 1:
        raise RuntimeError("Activation is restricted to a one-contract Phase 3M proposal.")
    return PaperActivationCandidate(
        ticker=ticker,
        forecast=forecast,
        snapshot=snapshot,
        ranking=ranking,
        gate_row=gate_row,
        preflight_row=preflight_row,
        pair_key=pair_key,
    )


def activate_one_contract_paper_order(
    session: Session,
    *,
    candidate: PaperActivationCandidate,
    historical_cache_path: Path,
    settings: Settings,
    execute: bool,
    operator_approval: str,
) -> dict[str, Any]:
    phrase = required_approval_phrase(candidate.ticker)
    if not execute:
        return {
            "status": "READY_AWAITING_EXPLICIT_EXECUTE",
            "ticker": candidate.ticker,
            "required_approval_phrase": phrase,
            "pair_key": candidate.pair_key,
            "order_created": False,
        }
    if operator_approval != phrase:
        raise RuntimeError("Exact ticker-specific operator approval phrase is required.")
    existing = session.scalar(
        select(PaperOrder)
        .where(
            PaperOrder.ticker == candidate.ticker,
            PaperOrder.model_name == "weather_v2",
            PaperOrder.forecast_id == candidate.forecast.id,
        )
        .limit(1)
    )
    if existing is not None:
        existing_fill = session.scalar(
            select(PaperFill).where(PaperFill.paper_order_id == existing.id).limit(1)
        )
        return {
            "status": "IDEMPOTENT_EXISTING_ORDER",
            "ticker": candidate.ticker,
            "paper_order_id": existing.id,
            "paper_fill_id": existing_fill.id if existing_fill is not None else None,
            "pair_key": candidate.pair_key,
            "order_created": False,
        }
    cache = _read_json(historical_cache_path)
    raw = {
        "source": ACTIVATION_VERSION,
        "strategy": "weather_v2_operator_approved_one_contract",
        "forecast_snapshot_pair_key": candidate.pair_key,
        "operator_approval_phrase": operator_approval,
        "explicit_operator_approval": True,
        "paper_only": True,
        "live_execution_enabled": False,
        "position_sizing_historical_evidence_cache": _cache_for_forecast(
            cache, candidate.forecast
        ),
    }
    probability = to_decimal(candidate.forecast.yes_probability)
    price = to_decimal(candidate.ranking.best_price)
    edge = to_decimal(candidate.ranking.estimated_edge)
    if probability is None or price is None or edge is None:
        raise RuntimeError("Activation candidate lacks probability, price, or edge.")
    side = _buy_side(candidate.ranking.best_side)
    decision = PaperDecision(
        ticker=candidate.ticker,
        forecast_id=candidate.forecast.id,
        model_name="weather_v2",
        side=side,
        probability=probability,
        market_price=price,
        limit_price=price,
        edge=edge,
        quantity=1,
        reason="Explicitly approved one-contract paper activation; live execution remains disabled.",
        raw_decision_json=raw,
    )
    activation_settings = settings.model_copy(
        update={
            "execution_enabled": False,
            "execution_dry_run": True,
            "paper_order_creation_enabled": True,
            "paper_order_kill_switch": False,
            "paper_max_order_quantity": 1,
            "dynamic_position_sizing_mode": "shadow",
            "advanced_risk_engine_mode": "shadow",
        }
    )
    order = create_paper_order(session, decision, settings=activation_settings)
    if order is None or order.quantity != 1:
        raise RuntimeError("Paper ledger declined the one-contract activation.")
    fill = simulate_immediate_fill(session, order, settings=activation_settings)
    if fill is None or fill.quantity != 1:
        raise RuntimeError("Paper simulator declined the one-contract immediate fill.")
    return {
        "status": "ONE_CONTRACT_PAPER_ORDER_CREATED",
        "ticker": candidate.ticker,
        "paper_order_id": order.id,
        "paper_fill_id": fill.id,
        "forecast_id": candidate.forecast.id,
        "snapshot_id": candidate.snapshot.id,
        "pair_key": candidate.pair_key,
        "quantity": order.quantity,
        "fill_price": fill.price,
        "order_created": True,
        "live_execution_enabled": False,
    }


def _ticker_row(payload: dict[str, Any], ticker: str, key: str) -> dict[str, Any]:
    row = next((item for item in payload.get(key, []) if item.get("ticker") == ticker), None)
    if row is None:
        raise RuntimeError(f"Artifact lacks ticker {ticker} in {key}.")
    return row


def _cache_for_forecast(cache: dict[str, Any], forecast: Forecast) -> dict[str, Any]:
    source = next(
        (
            entry
            for entry in (cache.get("entries") or {}).values()
            if entry.get("ticker") == forecast.ticker
            and entry.get("model_name") == forecast.model_name
        ),
        None,
    )
    if source is None:
        raise RuntimeError("Historical evidence cache does not cover the activation forecast.")
    return {
        **cache,
        "entries": {
            str(forecast.id): {**source, "forecast_id": forecast.id},
        },
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Required activation artifact is unavailable: {path}") from error


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _buy_side(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"YES", BUY_YES}:
        return BUY_YES
    if normalized in {"NO", BUY_NO}:
        return BUY_NO
    raise RuntimeError("Activation ranking lacks a recognized executable BUY side.")
