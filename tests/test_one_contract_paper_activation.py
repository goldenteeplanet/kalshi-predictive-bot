from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import (
    Base,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
)
from kalshi_predictor.paper.activation import (
    activate_one_contract_paper_order,
    required_approval_phrase,
    validate_one_contract_paper_activation,
)

TICKER = "KXRAINAUSM-26AUG-T1.5"


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _candidate(tmp_path):
    now = datetime.now(UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(
        Market(
            ticker=TICKER,
            status="active",
            raw_json="{}",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    snapshot = MarketSnapshot(
        ticker=TICKER,
        captured_at=now - timedelta(seconds=20),
        status="active",
        best_yes_ask="0.42",
        raw_market_json="{}",
        raw_orderbook_json="{}",
    )
    session.add(snapshot)
    session.flush()
    forecast = Forecast(
        ticker=TICKER,
        forecasted_at=now - timedelta(seconds=15),
        model_name="weather_v2",
        yes_probability="0.55",
        feature_json="{}",
    )
    session.add(forecast)
    session.flush()
    ranking = MarketRanking(
        ticker=TICKER,
        ranked_at=now - timedelta(seconds=10),
        status="active",
        forecast_model="weather_v2",
        forecast_probability="0.55",
        best_side="YES",
        best_price="0.42",
        estimated_edge="0.10",
        liquidity_score="1",
        spread_score="1",
        time_score="1",
        model_confidence_score="1",
        opportunity_score="1",
        reason="test",
        raw_json="{}",
    )
    session.add(ranking)
    session.commit()
    pair_key = f"forecast:{forecast.id}:snapshot:{snapshot.id}"
    soak = tmp_path / "soak.json"
    gate = tmp_path / "gate.json"
    preflight = tmp_path / "preflight.json"
    pair_state = tmp_path / "pairs.json"
    cache = tmp_path / "cache.json"
    _write(
        soak,
        {
            "soak_complete": True,
            "consecutive_healthy_cycles": 3,
            "explicit_operator_approval_required": True,
            "paper_order_creation_enabled": False,
        },
    )
    _write(
        gate,
        {
            "weather_rows": [
                {
                    "ticker": TICKER,
                    "paper_ready": True,
                    "first_blocker": "PAPER_READY",
                    "ranking_id": ranking.id,
                    "forecast_id": forecast.id,
                }
            ]
        },
    )
    _write(
        preflight,
        {
            "results": [
                {
                    "ticker": TICKER,
                    "status": "RECORDED",
                    "phase3n_action": "ALLOW",
                    "phase3n_hard_blocks": [],
                    "forecast_snapshot_pair_key": pair_key,
                    "buy_price": "0.42",
                    "phase3m_proposed_contracts": 1,
                }
            ]
        },
    )
    _write(pair_state, {"completed_pair_keys": [pair_key]})
    _write(
        cache,
        {
            "entries": {
                "old": {"ticker": TICKER, "model_name": "weather_v2", "evidence": {}}
            }
        },
    )
    settings = Settings(advanced_risk_quote_max_age_ms=120_000)
    candidate = validate_one_contract_paper_activation(
        session,
        ticker=TICKER,
        soak_path=soak,
        gate_path=gate,
        preflight_path=preflight,
        pair_state_path=pair_state,
        settings=settings,
        now=now,
    )
    return session, candidate, settings, cache, gate


def test_preview_is_read_only_and_discloses_exact_approval_phrase(tmp_path) -> None:
    session, candidate, settings, cache, _ = _candidate(tmp_path)
    result = activate_one_contract_paper_order(
        session,
        candidate=candidate,
        historical_cache_path=cache,
        settings=settings,
        execute=False,
        operator_approval="",
    )
    assert result["status"] == "READY_AWAITING_EXPLICIT_EXECUTE"
    assert result["required_approval_phrase"] == required_approval_phrase(TICKER)
    assert session.scalar(select(func.count(PaperOrder.id))) == 0


def test_execute_rejects_inexact_operator_approval_without_order(tmp_path) -> None:
    session, candidate, settings, cache, _ = _candidate(tmp_path)
    with pytest.raises(RuntimeError, match="Exact ticker-specific"):
        activate_one_contract_paper_order(
            session,
            candidate=candidate,
            historical_cache_path=cache,
            settings=settings,
            execute=True,
            operator_approval="AUTHORIZE",
        )
    assert session.scalar(select(func.count(PaperOrder.id))) == 0


def test_existing_forecast_order_is_idempotent_without_second_creation(tmp_path) -> None:
    session, candidate, settings, cache, _ = _candidate(tmp_path)
    existing = PaperOrder(
        ticker=TICKER,
        forecast_id=candidate.forecast.id,
        created_at=datetime.now(UTC),
        model_name="weather_v2",
        side="YES",
        probability="0.55",
        market_price="0.42",
        limit_price="0.42",
        edge="0.10",
        quantity=1,
        status="open",
        reason="existing test order",
        raw_decision_json="{}",
    )
    session.add(existing)
    session.commit()
    result = activate_one_contract_paper_order(
        session,
        candidate=candidate,
        historical_cache_path=cache,
        settings=settings,
        execute=True,
        operator_approval=required_approval_phrase(TICKER),
    )
    assert result["status"] == "IDEMPOTENT_EXISTING_ORDER"
    assert result["order_created"] is False
    assert session.scalar(select(func.count(PaperOrder.id))) == 1


def test_exact_approval_reaches_ledger_with_one_contract_and_live_disabled(
    tmp_path, monkeypatch
) -> None:
    session, candidate, settings, cache, _ = _candidate(tmp_path)
    observed = {}

    def fake_create_paper_order(_session, decision, *, settings):
        observed["decision"] = decision
        observed["settings"] = settings
        return PaperOrder(
            id=999,
            ticker=decision.ticker,
            forecast_id=decision.forecast_id,
            created_at=datetime.now(UTC),
            model_name=decision.model_name,
            side=decision.side,
            probability=str(decision.probability),
            market_price=str(decision.market_price),
            limit_price=str(decision.limit_price),
            edge=str(decision.edge),
            quantity=decision.quantity,
            status="open",
            reason=decision.reason,
            raw_decision_json="{}",
        )

    monkeypatch.setattr(
        "kalshi_predictor.paper.activation.create_paper_order", fake_create_paper_order
    )
    monkeypatch.setattr(
        "kalshi_predictor.paper.activation.simulate_immediate_fill",
        lambda _session, order, *, settings: PaperFill(
            id=999,
            paper_order_id=order.id,
            ticker=order.ticker,
            filled_at=datetime.now(UTC),
            side=order.side,
            price=order.limit_price,
            quantity=order.quantity,
            fee="0",
            raw_fill_json="{}",
        ),
    )
    result = activate_one_contract_paper_order(
        session,
        candidate=candidate,
        historical_cache_path=cache,
        settings=settings,
        execute=True,
        operator_approval=required_approval_phrase(TICKER),
    )
    assert result["status"] == "ONE_CONTRACT_PAPER_ORDER_CREATED"
    assert observed["decision"].quantity == 1
    assert observed["decision"].side == "BUY_YES"
    assert observed["settings"].execution_enabled is False
    assert observed["settings"].execution_dry_run is True
    assert observed["settings"].paper_max_order_quantity == 1


def test_current_paper_ready_is_required(tmp_path) -> None:
    session, _, settings, _, gate = _candidate(tmp_path)
    payload = json.loads(gate.read_text(encoding="utf-8"))
    payload["weather_rows"][0]["paper_ready"] = False
    payload["weather_rows"][0]["first_blocker"] = "QUOTE_STALE"
    _write(gate, payload)
    with pytest.raises(RuntimeError, match="not currently PAPER_READY"):
        validate_one_contract_paper_activation(
            session,
            ticker=TICKER,
            soak_path=tmp_path / "soak.json",
            gate_path=gate,
            preflight_path=tmp_path / "preflight.json",
            pair_state_path=tmp_path / "pairs.json",
            settings=settings,
        )
