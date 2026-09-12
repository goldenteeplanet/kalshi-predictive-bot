import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "scripts/positive_ev_crypto_research.py"
spec = importlib.util.spec_from_file_location("bounded_crypto_runner", path)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_default_scope_preserves_twelve_request_cap():
    symbols, pages, maximum = runner.capture_scope()
    assert len(symbols) == 5 and pages == 1 and maximum == 12


def test_extended_single_symbol_scope_and_legacy_sol():
    assert runner.capture_scope(symbol="XRP", history_pages=4) == ((("XRP", "KXXRP"),), 4, 7)
    assert runner.capture_scope(sol_history=True) == ((("SOL", "KXSOLE"),), 4, 7)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(history_pages=5),
        dict(history_pages=True),
        dict(history_pages=4),
        dict(symbol="UNKNOWN"),
        dict(sol_history=True, symbol="XRP"),
    ],
)
def test_invalid_scope_refuses_before_creating_output(tmp_path, kwargs):
    output = tmp_path / "capture"
    with pytest.raises(ValueError):
        runner.main(output, **kwargs)
    assert not output.exists()


def test_fixed_target_includes_both_books_without_listing_edges():
    target = datetime(2030, 1, 1, tzinfo=UTC)
    inputs = {t: {"target": {"observation_at": target}} for t in ("B", "A")}
    rows = [{"ticker": t, "indicative_gross_edge": None} for t in inputs]
    assert runner.select_book_tickers(inputs, rows, target_at=target) == ["A", "B"]
    assert runner.select_book_tickers(inputs, rows) == []
    inputs["A"]["target"]["observation_at"] += timedelta(hours=1)
    with pytest.raises(ValueError, match="TARGET_SELECTION"):
        runner.select_book_tickers(inputs, rows, target_at=target)


@pytest.mark.parametrize(
    "value,symbol",
    [
        ("2030-01-01T00:00:00", "SOL"),
        ("2000-01-01T00:00:00Z", "SOL"),
        ("2030-01-01T00:00:00Z", None),
    ],
)
def test_bad_target_refuses_before_output_creation(tmp_path, value, symbol):
    output = tmp_path / "capture"
    with pytest.raises(ValueError):
        runner.main(output, symbol=symbol, target_at=value)
    assert not output.exists()


def test_target_normalization_preserves_exact_instant():
    value = runner.fixed_target(
        "2030-01-01T01:00:00+01:00", symbol="SOL", as_of=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert value == datetime(2030, 1, 1, tzinfo=UTC)
