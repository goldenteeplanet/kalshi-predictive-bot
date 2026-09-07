from dataclasses import replace

import pytest

from kalshi_predictor.roadmap.execution_gateway import (
    ApprovedOrderIntent,
    SimulatedExecutionGateway,
)


def _intent(**overrides) -> ApprovedOrderIntent:
    values = {
        "intent_id": "intent-1",
        "ticker": "KXBTC-TEST",
        "category": "crypto",
        "side": "yes",
        "action": "buy",
        "quantity": 1,
        "limit_price_cents": 55,
        "phase_3n_approved": True,
        "operator_confirmed": True,
        "idempotency_key": "paper-to-sim-1",
    }
    values.update(overrides)
    return ApprovedOrderIntent(**values)


def test_simulated_gateway_fills_one_contract_without_network() -> None:
    gateway = SimulatedExecutionGateway(account_id="demo-1")

    acknowledgement = gateway.submit_order(_intent())

    assert acknowledgement["status"] == "FILLED"
    assert acknowledgement["mode"] == "SIMULATED"
    assert acknowledgement["network_call_performed"] is False
    assert gateway.account_snapshot()["authenticated"] is False
    assert gateway.fills()[0]["quantity"] == 1
    assert gateway.positions() == [
        {"ticker": "KXBTC-TEST", "side": "yes", "quantity": 1, "mode": "SIMULATED"}
    ]


def test_simulated_gateway_idempotency_is_deterministic() -> None:
    first_gateway = SimulatedExecutionGateway()
    second_gateway = SimulatedExecutionGateway()
    intent = _intent()

    first = first_gateway.submit_order(intent)
    replay = first_gateway.submit_order(intent)
    independently_recreated = second_gateway.submit_order(intent)

    assert replay["order_id"] == first["order_id"]
    assert replay["idempotent_replay"] is True
    assert len(first_gateway.orders()) == 1
    assert len(first_gateway.fills()) == 1
    assert independently_recreated["order_id"] == first["order_id"]


def test_simulated_gateway_rejects_conflict_and_non_one_contract_order() -> None:
    gateway = SimulatedExecutionGateway()
    intent = _intent()
    gateway.submit_order(intent)

    with pytest.raises(ValueError, match="different order intent"):
        gateway.submit_order(replace(intent, ticker="KXBTC-OTHER"))
    with pytest.raises(ValueError, match="exactly one contract"):
        gateway.submit_order(_intent(quantity=2, idempotency_key="two-contracts"))


def test_simulated_gateway_cancel_methods_are_safe_for_immediate_fills() -> None:
    gateway = SimulatedExecutionGateway()
    acknowledgement = gateway.submit_order(_intent())

    assert gateway.cancel_order(acknowledgement["order_id"])["status"] == "NOT_CANCELABLE"
    assert gateway.cancel_order("missing")["status"] == "NOT_FOUND"
    assert gateway.cancel_all() == {
        "status": "COMPLETE",
        "canceled_order_ids": [],
        "mode": "SIMULATED",
    }
