from __future__ import annotations

from decimal import Decimal

from scripts.local.phase4nf_fee_schedule import compare_flat_fee, fee_envelope, select_schedule


def _schedule(
    version=1,
    start="2026-01-01T00:00:00Z",
    end="2026-09-01T00:00:00Z",
    published="2025-12-01T00:00:00Z",
    **overrides,
):
    row = {
        "version": version,
        "effective_from": start,
        "effective_until": end,
        "published_at": published,
        "maker_rate": "0.02",
        "taker_rate": "0.07",
        "minimum_fee": "0.01",
        "maximum_fee": "2.00",
        "rebate_rate": "0.10",
        "surcharge_rate": "0.05",
        "cancel_fee": "0.01",
        "settlement_fee": "0.005",
        "rounding_increment": "0.01",
    }
    row.update(overrides)
    return row


def _trade(**overrides):
    row = {
        "order_time": "2026-08-01T00:00:00Z",
        "liquidity_role": "ambiguous",
        "price": "0.55",
        "quantity": "10",
        "partial_fills": ["4", "6"],
        "cancel_replace_count": 1,
        "settled": True,
    }
    row.update(overrides)
    return row


def test_effective_published_schedule_and_envelope_are_deterministic() -> None:
    schedules = [_schedule()]
    first = fee_envelope(_trade(), schedules)
    assert first == fee_envelope(_trade(), schedules)
    assert first["verdict"] == "PASS"
    assert first["selected_version"] == 1
    assert first["worst_case_role"] == "taker"
    assert Decimal(first["envelopes"]["worst_case"]["total_fee"]) >= Decimal(
        first["envelopes"]["baseline"]["total_fee"]
    )


def test_future_publication_retroactive_revision_and_schedule_gap_refuse() -> None:
    future = _schedule(published="2026-08-02T00:00:00Z")
    assert (
        "NO_PUBLICLY_EFFECTIVE_SCHEDULE"
        in select_schedule([future], order_time=_trade()["order_time"])["errors"]
    )
    gap = [
        _schedule(end="2026-07-01T00:00:00Z"),
        _schedule(2, start="2026-08-02T00:00:00Z", end="2027-01-01T00:00:00Z"),
    ]
    assert (
        "NO_PUBLICLY_EFFECTIVE_SCHEDULE"
        in select_schedule(gap, order_time=_trade()["order_time"])["errors"]
    )


def test_overlapping_versions_refuse() -> None:
    schedules = [
        _schedule(),
        _schedule(2, start="2026-07-01T00:00:00Z", end="2027-01-01T00:00:00Z"),
    ]
    assert (
        "OVERLAPPING_EFFECTIVE_SCHEDULES"
        in select_schedule(schedules, order_time=_trade()["order_time"])["errors"]
    )


def test_maker_taker_ambiguity_uses_more_expensive_verified_role() -> None:
    result = fee_envelope(_trade(liquidity_role="ambiguous"), [_schedule()])
    assert result["baseline_role"] == "maker"
    assert result["worst_case_role"] == "taker"
    explicit = fee_envelope(_trade(liquidity_role="maker"), [_schedule()])
    assert explicit["baseline_role"] == explicit["worst_case_role"] == "maker"


def test_rounding_minimum_caps_tiny_and_large_orders() -> None:
    schedule = _schedule(maximum_fee="0.50")
    tiny = fee_envelope(_trade(quantity="0.01", partial_fills=["0.01"], settled=False), [schedule])
    assert Decimal(tiny["envelopes"]["worst_case"]["trading_fee"]) >= Decimal("0.01")
    large = fee_envelope(
        _trade(quantity="10000", partial_fills=["10000"], settled=False), [schedule]
    )
    assert Decimal(large["envelopes"]["worst_case"]["trading_fee"]) <= Decimal("0.50")


def test_partial_fills_cancel_replace_and_settlement_costs_are_included() -> None:
    result = fee_envelope(_trade(), [_schedule()])["envelopes"]["worst_case"]
    assert len(result["components"]) == 2
    assert Decimal(result["cancel_fees"]) > 0
    assert Decimal(result["settlement_fees"]) > 0
    assert result["per_fill_minimum"] is True


def test_rebate_above_charge_and_negative_fee_refuse() -> None:
    excessive = fee_envelope(_trade(liquidity_role="maker"), [_schedule(rebate_rate="10")])
    assert "REBATE_EXCEEDS_CHARGES" in excessive["errors"]
    negative = fee_envelope(_trade(), [_schedule(taker_rate="-0.01")])
    assert "NEGATIVE_OR_INVALID_FEE" in " ".join(negative["errors"])


def test_invalid_partial_fills_and_role_refuse() -> None:
    assert fee_envelope(_trade(partial_fills=["11"]), [_schedule()])["verdict"] == "REFUSE"
    assert (
        "LIQUIDITY_ROLE_INVALID"
        in fee_envelope(_trade(liquidity_role="unknown"), [_schedule()])["errors"]
    )


def test_verified_worst_cost_quantifies_flat_fee_pnl_inflation_and_readiness() -> None:
    envelope = fee_envelope(_trade(), [_schedule()])
    comparison = compare_flat_fee(
        envelope, flat_fee_per_contract="0.001", quantity="10", gross_pnl="0.20"
    )
    assert Decimal(comparison["pnl_inflation"]) > 0
    assert comparison["readiness"] == "REFUSE"
    assert comparison["net_edge_survives"] is False


def test_fee_model_has_no_order_or_execution_capability() -> None:
    safety = fee_envelope(_trade(), [_schedule()])["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
