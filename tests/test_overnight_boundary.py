from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.overnight_paper.boundary import (
    ExecutionMode,
    LocalPaperAuthorization,
    require_local_mode,
    validate_authorization,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)
AUTH = LocalPaperAuthorization(NOW, NOW + timedelta(hours=24), "a" * 64)


def validate(**overrides):
    values = dict(
        now=NOW,
        contracts=1,
        new_positions=0,
        open_positions=0,
        event_id="crypto-event",
        used_events=frozenset(),
        settlement_at=NOW + timedelta(hours=1),
    )
    values.update(overrides)
    return validate_authorization(AUTH, **values)


@pytest.mark.parametrize(
    "mode",
    [ExecutionMode.OBSERVATION_ONLY, ExecutionMode.DEMO_EXCHANGE, ExecutionMode.LIVE_EXCHANGE],
)
def test_only_local_mode_can_create(mode):
    with pytest.raises(PermissionError):
        require_local_mode(mode)
    assert "LOCAL_PAPER_ONLY" in validate_authorization(
        replace(AUTH, mode=mode),
        now=NOW,
        contracts=1,
        new_positions=0,
        open_positions=0,
        event_id="e",
        used_events=frozenset(),
        settlement_at=NOW + timedelta(hours=1),
    )


def test_authorized_local_position():
    require_local_mode(ExecutionMode.LOCAL_PAPER)
    assert validate() == ()


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("contracts", 2, "ONE_CONTRACT_MAXIMUM"),
        ("contracts", True, "ONE_CONTRACT_MAXIMUM"),
        ("new_positions", 3, "NEW_POSITION_LIMIT"),
        ("open_positions", 3, "OPEN_POSITION_LIMIT"),
        ("used_events", frozenset({"crypto-event"}), "INDEPENDENT_EVENT_REQUIRED"),
        ("settlement_at", NOW + timedelta(hours=73), "SETTLEMENT_HORIZON_EXCEEDED"),
        ("settlement_at", NOW, "SETTLEMENT_HORIZON_EXCEEDED"),
        ("now", NOW + timedelta(days=1), "AUTHORIZATION_NOT_CURRENT"),
        ("now", NOW.replace(tzinfo=None), "TIMEZONE_REQUIRED"),
    ],
)
def test_goal_limits(field, value, reason):
    assert reason in validate(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_contracts_per_position", 2),
        ("max_new_positions", 4),
        ("max_open_positions", 4),
        ("max_positions_per_event", 2),
        ("hard_horizon_hours", 73),
    ],
)
def test_cannot_expand_authorization(field, value):
    assert "AUTHORIZATION_LIMITS_EXCEEDED" in validate_authorization(
        replace(AUTH, **{field: value}),
        now=NOW,
        contracts=1,
        new_positions=0,
        open_positions=0,
        event_id="e",
        used_events=frozenset(),
        settlement_at=NOW + timedelta(hours=1),
    )


def test_boundary_and_qualification_have_no_transport_or_gateway_imports():
    import ast
    from pathlib import Path

    from kalshi_predictor.overnight_paper import boundary, qualification

    for module in (boundary, qualification):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            assert not any(
                any(
                    token in name
                    for token in (
                        "httpx",
                        "requests",
                        "socket",
                        "gateway",
                        "kalshi.client",
                        "execution",
                    )
                )
                for name in imported
            )


def test_existing_fill_simulator_has_only_local_ledger_calls():
    import ast
    from pathlib import Path

    from kalshi_predictor.paper import simulator

    tree = ast.parse(Path(simulator.__file__).read_text())
    fill = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "simulate_immediate_fill"
    )
    calls = {
        node.func.id
        for node in ast.walk(fill)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert calls <= {
        "get_settings",
        "to_decimal",
        "int",
        "Decimal",
        "utc_now",
        "insert_paper_fill",
        "mark_order_filled",
        "update_position_for_fill",
    }
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr
        in {
            "post",
            "delete",
            "request",
            "submit_order",
            "cancel_order",
            "account_snapshot",
        }
        for node in ast.walk(fill)
    )
