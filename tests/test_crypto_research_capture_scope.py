import importlib.util
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
