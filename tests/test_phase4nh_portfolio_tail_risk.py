from __future__ import annotations

import copy
from decimal import Decimal

from scripts.local.phase4nh_portfolio_tail_risk import analyze_portfolio


def _position(identifier, factor, exposure, expected="0.20", signature=None, **overrides):
    row = {
        "position_id": identifier,
        "event_group": f"event-{factor}",
        "underlying": factor,
        "geography": "global",
        "time_window": "day-1",
        "data_source": "source-a",
        "model_family": "model-a",
        "economic_exposure_signature": signature or identifier,
        "factor_exposures": {factor: str(exposure)},
        "expected_pnl": expected,
        "worst_case_fee": "0.05",
        "pessimistic_fill_verified": True,
        "worst_case_fee_verified": True,
    }
    row.update(overrides)
    return row


def _snapshot(positions=None):
    return {
        "captured_at": "2026-08-01T11:59:30Z",
        "positions": positions
        if positions is not None
        else [_position("existing", "rain", 1, expected="0.10")],
    }


def _scenarios():
    return [
        {"name": "base", "factor_shocks": {}},
        {"name": "rain-down", "factor_shocks": {"rain": "-1", "crypto": "0"}},
        {"name": "crypto-down", "factor_shocks": {"rain": "0", "crypto": "-1"}},
        {"name": "joint-down", "factor_shocks": {"rain": "-1", "crypto": "-1"}},
        {
            "name": "hedge-failure",
            "factor_shocks": {"rain": "-1", "crypto": "1"},
            "hedge_failure": True,
        },
    ]


def _analyze(proposed=None, snapshot=None, **overrides):
    values = {
        "evaluated_at": "2026-08-01T12:00:00Z",
        "maximum_snapshot_age_seconds": 60,
        "maximum_joint_loss": "5",
        "maximum_concentration": "0.80",
    }
    values.update(overrides)
    return analyze_portfolio(
        snapshot or _snapshot(),
        proposed or [_position("proposed", "crypto", 1)],
        _scenarios(),
        **values,
    )


def test_portfolio_analysis_is_deterministic_and_reports_joint_tail_metrics() -> None:
    first = _analyze()
    assert first == _analyze()
    assert first["verdict"] == "PASS"
    for field in (
        "worst_case_pnl",
        "expected_shortfall_95",
        "perfect_correlation_loss",
        "covariance_stress",
        "concentration",
        "marginal_risk_contribution",
        "drawdown_consumption",
        "remaining_risk_capacity",
    ):
        assert field in first


def test_individually_profitable_trades_can_fail_joint_loss_limit() -> None:
    proposed = [
        _position("p1", "rain", 3, expected="0.5"),
        _position("p2", "rain", 3, expected="0.5"),
    ]
    result = _analyze(proposed, maximum_joint_loss="2")
    assert all(Decimal(row["expected_pnl"]) > 0 for row in proposed)
    assert result["readiness"] == "REFUSE"
    assert "JOINT_TAIL_LOSS_LIMIT_BREACHED" in result["readiness_errors"]
    assert result["order_authorized"] is False


def test_perfect_correlation_override_and_concentration_refuse() -> None:
    proposed = [_position("p1", "rain", 2), _position("p2", "rain", 2)]
    result = _analyze(proposed, maximum_concentration="0.50")
    assert Decimal(result["perfect_correlation_loss"]) < 0
    assert "PORTFOLIO_CONCENTRATION_LIMIT_BREACHED" in result["readiness_errors"]


def test_hedge_failure_basis_and_settlement_mismatch_remain_visible() -> None:
    hedge = _position("hedge", "crypto", -1, time_window="day-2")
    result = _analyze([hedge])
    row = next(item for item in result["scenario_pnl"] if item["name"] == "hedge-failure")
    assert Decimal(row["pnl"]) < Decimal(
        next(item for item in result["scenario_pnl"] if item["name"] == "base")["pnl"]
    )
    assert set(result["group_exposure"]["time_window"]) == {"day-1", "day-2"}


def test_common_source_and_model_exposure_are_grouped() -> None:
    result = _analyze([_position("p1", "crypto", 1), _position("p2", "rain", 1)])
    assert set(result["group_exposure"]["data_source"]) == {"source-a"}
    assert set(result["group_exposure"]["model_family"]) == {"model-a"}


def test_stale_snapshot_missing_positions_duplicates_and_unverified_trades_refuse() -> None:
    stale = _snapshot()
    stale["captured_at"] = "2026-08-01T11:00:00Z"
    assert "STALE_OR_INVALID_PORTFOLIO_SNAPSHOT" in _analyze(snapshot=stale)["errors"]
    assert (
        "POSITIONS_MISSING" in _analyze(snapshot={"captured_at": "2026-08-01T11:59:30Z"})["errors"]
    )
    duplicate = _position("existing", "crypto", 1)
    assert "DUPLICATE_POSITION_OR_ORDER" in " ".join(_analyze([duplicate])["errors"])
    unverified = _position("bad", "crypto", 1, pessimistic_fill_verified=False)
    assert "PROPOSED_TRADE_NOT_PESSIMISTICALLY_VERIFIED" in " ".join(
        _analyze([unverified])["errors"]
    )


def test_duplicated_economic_exposure_is_detected_even_across_tickers() -> None:
    proposed = [
        _position("p1", "crypto", 1, signature="same"),
        _position("p2", "crypto", 1, signature="same"),
    ]
    result = _analyze(proposed)
    assert result["readiness"] == "REFUSE"
    assert "DUPLICATED_ECONOMIC_EXPOSURE" in result["readiness_errors"]


def test_partial_fill_exposure_is_used_exactly_as_supplied() -> None:
    partial = _position("partial", "crypto", "0.5")
    full = _position("full", "crypto", "1")
    partial_result, full_result = _analyze([partial]), _analyze([full])
    assert Decimal(partial_result["factor_exposure"]["crypto"]) < Decimal(
        full_result["factor_exposure"]["crypto"]
    )


def test_proposed_trade_ordering_is_deterministic() -> None:
    proposed = [_position("z", "crypto", 1), _position("a", "weather", 1)]
    first = _analyze(proposed)
    second = _analyze(list(reversed(copy.deepcopy(proposed))))
    assert first["ordered_proposed_position_ids"] == ["a", "z"]
    assert first["portfolio_audit_sha256"] == second["portfolio_audit_sha256"]


def test_portfolio_model_has_no_order_or_execution_capability() -> None:
    result = _analyze()
    assert result["order_authorized"] is False
    safety = result["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
