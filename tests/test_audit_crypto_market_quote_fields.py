import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "audit_crypto_market_quote_fields.py"
    spec = importlib.util.spec_from_file_location("audit_crypto_market_quote_fields", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUDIT = _module()


def test_top_level_dollar_quotes_fill_empty_book():
    row = AUDIT.audit_bucket(
        {"ticker": "TEST", "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42"},
        {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}},
    )
    assert row["classification"] == "TOP_LEVEL_EXECUTABLE_ONLY"
    assert row["top_yes_bid"] == "0.40"


def test_top_level_cent_quotes_are_normalized():
    row = AUDIT.audit_bucket(
        {"ticker": "TEST", "yes_bid": 40, "yes_ask": 42},
        {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}},
    )
    assert row["classification"] == "TOP_LEVEL_EXECUTABLE_ONLY"
    assert row["top_yes_ask"] == "0.42"


def test_zero_quotes_and_empty_book_are_genuinely_unquoted():
    row = AUDIT.audit_bucket(
        {"ticker": "TEST", "yes_bid": 0, "yes_ask": 0},
        {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}},
    )
    assert row["classification"] == "GENUINELY_UNQUOTED"
