from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from kalshi_predictor import (
    phase3aa_r2,
    phase3aa_r3,
    phase3aa_r4,
    phase3aa_r5,
    phase3aa_r6,
)
from kalshi_predictor.backtesting import engine as backtesting_engine
from kalshi_predictor.crypto import backtest as crypto_backtest
from kalshi_predictor.historical_replay_common import (
    has_usable_outcome,
    is_local_derived_composite_ticker,
    markdown_cell_empty,
    markdown_cell_none,
    normalize_result,
    settlement_to_y_true,
    source_is_closed_without_outcome,
    source_is_settled,
    trade_from_decision,
)
from kalshi_predictor.tournament import engine as tournament_engine
from kalshi_predictor.weather import backtest as weather_backtest
from kalshi_predictor.wrapper_inventory import build_wrapper_inventory


@dataclass
class _Decision:
    ticker: str = "TEST"
    forecast_id: int = 1
    simulated_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    side: str = "BUY_YES"
    price: Decimal = Decimal("0.40")
    quantity: int = 1
    edge: Decimal = Decimal("0.10")
    yes_probability: Decimal = Decimal("0.50")


def test_replay_compatibility_aliases_use_canonical_helpers() -> None:
    assert backtesting_engine._settlement_to_y_true is settlement_to_y_true
    assert crypto_backtest._settlement_to_y_true is settlement_to_y_true
    assert weather_backtest._settlement_to_y_true is settlement_to_y_true
    assert tournament_engine._settlement_to_y_true is settlement_to_y_true
    assert crypto_backtest._trade_from_decision is trade_from_decision
    assert weather_backtest._trade_from_decision is trade_from_decision

    assert phase3aa_r2._has_usable_outcome is has_usable_outcome
    assert phase3aa_r6._has_usable_outcome is has_usable_outcome
    assert phase3aa_r2._source_is_closed_without_outcome is source_is_closed_without_outcome
    assert phase3aa_r6._source_is_closed_without_outcome is source_is_closed_without_outcome
    assert phase3aa_r2._source_is_settled is source_is_settled
    assert phase3aa_r6._source_is_settled is source_is_settled
    assert phase3aa_r2._is_local_derived_composite_ticker is is_local_derived_composite_ticker
    assert phase3aa_r6._is_local_derived_composite_ticker is is_local_derived_composite_ticker
    assert phase3aa_r3._normalize_result is normalize_result
    assert phase3aa_r6._normalize_result is normalize_result
    assert phase3aa_r2._md is markdown_cell_empty
    assert phase3aa_r6._md is markdown_cell_empty
    assert phase3aa_r4._md is markdown_cell_none
    assert phase3aa_r5._md is markdown_cell_none


def test_replay_helpers_preserve_settlement_and_trade_semantics() -> None:
    assert settlement_to_y_true("YES") == 1
    assert settlement_to_y_true("no") == 0
    assert settlement_to_y_true("scalar") is None
    assert normalize_result(" Y ") == "yes"
    assert source_is_closed_without_outcome({"status": "closed"})
    assert source_is_settled({"status": "resolved"})
    assert is_local_derived_composite_ticker("KXMVECROSSCATEGORY-TEST")

    row = trade_from_decision(
        _Decision(),
        y_true=1,
        settlement_result="yes",
        fee_per_contract=Decimal("0.01"),
    )
    assert row["pnl"] == "0.59"
    assert row["exposure"] == "0.41"


def test_historical_replay_scan_has_no_exact_duplicates() -> None:
    payload = build_wrapper_inventory()

    assert payload["status"] == "READY"
    assert payload["historical_replay_duplicate_helpers"] == []


def test_replay_common_has_no_current_paper_or_gh2_importers() -> None:
    root = Path(__file__).parents[1] / "src" / "kalshi_predictor"
    importers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "historical_replay_common.py"
        and "historical_replay_common" in path.read_text(encoding="utf-8")
    }

    assert importers
    assert not any(path.startswith("paper/") for path in importers)
    assert not any(
        "phase3bc" in path or "fixed_rate" in path or "gh2" in path for path in importers
    )
