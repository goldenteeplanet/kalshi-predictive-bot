from decimal import Decimal
from types import SimpleNamespace

from kalshi_predictor.paper.invariant_monitor import _expected_final_pnl


def test_yes_win_reconciles_exact_final_pnl_after_fee() -> None:
    order = SimpleNamespace(side="BUY_YES")
    fill = SimpleNamespace(price="0.2400", fee="0.01", quantity=1)
    assert _expected_final_pnl(order, fill, "yes") == Decimal("0.75")


def test_yes_loss_reconciles_exact_final_pnl_after_fee() -> None:
    order = SimpleNamespace(side="BUY_YES")
    fill = SimpleNamespace(price="0.2400", fee="0.01", quantity=1)
    assert _expected_final_pnl(order, fill, "no") == Decimal("-0.25")


def test_no_win_reconciles_exact_final_pnl_for_multiple_contracts() -> None:
    order = SimpleNamespace(side="BUY_NO")
    fill = SimpleNamespace(price="0.4000", fee="0.02", quantity=2)
    assert _expected_final_pnl(order, fill, "no") == Decimal("1.18")
