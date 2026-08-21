import importlib.util
from decimal import Decimal
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "shadow_paper_ledger.py"
SPEC = importlib.util.spec_from_file_location("shadow_paper_ledger", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def observation(**overrides):
    row = {
        "observation_id": "obs-1",
        "strategy_id": "canonical-r5-shadow",
        "strategy_version": "1",
        "captured_at": "2026-08-15T00:00:00+00:00",
        "market_ticker": "TEST-YES",
        "market_family": "TEST",
        "event_ticker": "TEST-EVENT",
        "forecast_timestamp": "2026-08-14T23:59:00+00:00",
        "snapshot_timestamp": "2026-08-14T23:59:30+00:00",
        "observable_ask": "0.40",
        "executable_side": "BUY_YES",
        "executable_price": "0.40",
        "executable_ev": "0.08",
        "book_usable": True,
        "production_blockers": [],
        "production_gates_all_passed": True,
    }
    row.update(overrides)
    return row


def test_accepts_only_positive_net_ev_after_costs():
    trade, reasons = MODULE.evaluate(observation(), fee=Decimal("0.01"), slippage=Decimal("0.01"))
    assert reasons == []
    assert trade["quantity"] == 1
    assert trade["fill_price"] == "0.41"
    assert trade["net_executable_ev"] == "0.06"
    assert trade["guarded_tables_written"] is False


def test_rejects_existing_production_blocker():
    trade, reasons = MODULE.evaluate(
        observation(production_blockers=["LOW_SCORE"], production_gates_all_passed=False),
        fee=Decimal("0.01"),
        slippage=Decimal("0.01"),
    )
    assert trade is None
    assert "PRODUCTION_GATE:LOW_SCORE" in reasons


def test_rejects_edge_consumed_by_realistic_cost_haircut():
    trade, reasons = MODULE.evaluate(
        observation(executable_ev="0.015"),
        fee=Decimal("0.01"),
        slippage=Decimal("0.01"),
    )
    assert trade is None
    assert "NET_EV_NOT_POSITIVE_AFTER_COSTS" in reasons
