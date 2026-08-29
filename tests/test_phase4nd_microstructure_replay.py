from __future__ import annotations

import copy
from decimal import Decimal

from scripts.local.phase4nd_microstructure_replay import compare_simple_fill, replay_order


def _events():
    return [
        {
            "sequence": 1,
            "timestamp": "2026-08-01T12:00:00.000Z",
            "type": "BOOK",
            "bid": "0.49",
            "ask": "0.51",
            "bid_size": 8,
            "ask_size": 4,
            "status": "open",
        },
        {
            "sequence": 2,
            "timestamp": "2026-08-01T12:00:00.150Z",
            "type": "TRADE",
            "price": "0.50",
            "size": 6,
        },
        {
            "sequence": 3,
            "timestamp": "2026-08-01T12:00:00.300Z",
            "type": "BOOK",
            "bid": "0.45",
            "ask": "0.55",
            "bid_size": 5,
            "ask_size": 5,
            "status": "open",
        },
    ]


def _order(**overrides):
    value = {
        "decision_time": "2026-08-01T12:00:00.000Z",
        "latency_ms": 50,
        "ack_delay_ms": 0,
        "max_book_age_ms": 100,
        "side": "yes",
        "limit_price": "0.52",
        "quantity": 5,
        "fee_per_contract": "0.01",
        "tick_size": "0.01",
        "burst_volume_ahead": 1,
        "hidden_liquidity": 0,
        "cancel_replace": False,
    }
    value.update(overrides)
    return value


def test_fill_envelopes_are_deterministic_ordered_and_pessimistic_for_readiness() -> None:
    first = replay_order(_events(), _order(), outcome="yes")
    assert first == replay_order(_events(), _order(), outcome="yes")
    assert first["verdict"] == "PASS"
    fills = [
        Decimal(first["envelopes"][name]["filled_quantity"])
        for name in ("optimistic", "central", "pessimistic")
    ]
    assert fills[0] >= fills[1] >= fills[2]
    assert first["readiness_envelope"] == "pessimistic"


def test_stale_crossed_closed_and_off_tick_books_refuse() -> None:
    assert "STALE_BOOK" in replay_order(_events(), _order(latency_ms=1000), outcome="yes")["errors"]
    crossed = _events()
    crossed[0]["bid"] = "0.52"
    assert "CROSSED_BOOK" in replay_order(crossed, _order(), outcome="yes")["errors"]
    closed = _events() + [{"sequence": 4, "timestamp": "2026-08-01T12:00:00.040Z", "type": "CLOSE"}]
    closed.sort(key=lambda row: row["timestamp"])
    for index, row in enumerate(closed):
        row["sequence"] = index + 1
    assert "MARKET_CLOSED" in replay_order(closed, _order(), outcome="yes")["errors"]
    assert (
        "PRICE_NOT_ON_TICK"
        in replay_order(_events(), _order(limit_price="0.515"), outcome="yes")["errors"]
    )


def test_missing_duplicate_sequence_and_time_reversal_refuse() -> None:
    missing = _events()
    missing[1]["sequence"] = 3
    assert "MISSING_OR_DUPLICATE_SEQUENCE" in " ".join(
        replay_order(missing, _order(), outcome="yes")["errors"]
    )
    reversed_events = _events()
    reversed_events[1]["timestamp"] = "2026-08-01T11:59:59Z"
    assert "TIME_REVERSED" in " ".join(
        replay_order(reversed_events, _order(), outcome="yes")["errors"]
    )


def test_hidden_liquidity_assumption_is_forbidden() -> None:
    result = replay_order(_events(), _order(hidden_liquidity=10), outcome="yes")
    assert result["verdict"] == "REFUSE"
    assert "HIDDEN_LIQUIDITY_ASSUMPTION_FORBIDDEN" in result["errors"]


def test_queue_ahead_partial_fills_and_queue_reset_are_pessimistic() -> None:
    order = _order(limit_price="0.50", quantity=5, latency_ms=50)
    base = replay_order(_events(), order, outcome="yes")
    assert Decimal(base["envelopes"]["optimistic"]["filled_quantity"]) > Decimal(
        base["envelopes"]["pessimistic"]["filled_quantity"]
    )
    reset = copy.deepcopy(_events())
    reset.insert(
        2,
        {
            "sequence": 3,
            "timestamp": "2026-08-01T12:00:00.200Z",
            "type": "QUEUE_RESET",
            "queue_ahead": 20,
        },
    )
    reset[3]["sequence"] = 4
    stressed = replay_order(reset, order, outcome="yes")
    assert Decimal(stressed["envelopes"]["pessimistic"]["filled_quantity"]) == 0


def test_cancel_replace_loses_priority_and_delayed_ack_sees_burst() -> None:
    normal = replay_order(_events(), _order(limit_price="0.50"), outcome="yes")
    replaced = replay_order(
        _events(), _order(limit_price="0.50", cancel_replace=True), outcome="yes"
    )
    delayed = replay_order(_events(), _order(ack_delay_ms=300), outcome="yes")
    assert Decimal(replaced["envelopes"]["pessimistic"]["filled_quantity"]) <= Decimal(
        normal["envelopes"]["pessimistic"]["filled_quantity"]
    )
    assert Decimal(delayed["envelopes"]["pessimistic"]["filled_quantity"]) <= Decimal(
        normal["envelopes"]["pessimistic"]["filled_quantity"]
    )


def test_depth_exhaustion_partial_fill_and_rejected_resting_order() -> None:
    crossing = replay_order(_events(), _order(quantity=10), outcome="yes")
    assert Decimal(crossing["envelopes"]["central"]["filled_quantity"]) == 4
    resting = replay_order(_events()[:1], _order(limit_price="0.48"), outcome="yes")
    assert Decimal(resting["envelopes"]["pessimistic"]["filled_quantity"]) == 0


def test_adverse_selection_and_simple_fill_inflation_are_quantified() -> None:
    replay = replay_order(_events(), _order(), outcome="yes")
    assert Decimal(replay["envelopes"]["pessimistic"].get("post_fill_adverse_selection", 0)) >= 0
    comparison = compare_simple_fill(
        replay, simple_price="0.51", quantity=5, fee_per_contract="0.01", side="yes", outcome="yes"
    )
    assert Decimal(comparison["simple_pnl_inflation"]) >= 0
    assert comparison["readiness_uses_simple"] is False


def test_burst_volume_reduces_pessimistic_crossing_fill() -> None:
    calm = replay_order(_events(), _order(burst_volume_ahead=0), outcome="yes")
    burst = replay_order(_events(), _order(burst_volume_ahead=4), outcome="yes")
    assert Decimal(burst["envelopes"]["pessimistic"]["filled_quantity"]) < Decimal(
        calm["envelopes"]["pessimistic"]["filled_quantity"]
    )


def test_replay_has_no_order_or_execution_capability() -> None:
    result = replay_order(_events(), _order(), outcome="yes")
    assert result["submitted_order"] is False
    safety = result["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
