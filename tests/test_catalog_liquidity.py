from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.crypto.catalog_liquidity import catalog_liquidity
from kalshi_predictor.kalshi.orderbook import usable_bid_ask_book

NOW = datetime(2026, 9, 12, 6, tzinfo=UTC)
MARKET = {"volume_fp": "1000", "open_interest_fp": "500", "liquidity_dollars": "0"}


def bound(market=None, **kwargs):
    return catalog_liquidity(
        MARKET if market is None else market,
        catalog_sha256="a" * 64,
        received_at=kwargs.get("received_at", NOW),
        decision_at=kwargs.get("decision_at", NOW),
    )


def test_binding_uses_existing_formula_and_preserves_zero():
    result = bound()
    assert result["score"] == Decimal(60)
    assert result["inputs"]["liquidity_dollars"] == "0"
    assert result["catalog_sha256"] == "a" * 64
    assert bound({key: "0" for key in MARKET})["score"] == 0


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "-1", "garbage"])
def test_invalid_input_remains_unknown(value):
    assert bound({**MARKET, "volume_fp": value})["score"] is None


@pytest.mark.parametrize("age", [-1, 60.001])
def test_future_and_stale_catalog_remain_unknown(age):
    assert bound(decision_at=NOW + timedelta(seconds=age))["score"] is None


def test_exact_freshness_boundary_and_naive_clock():
    assert bound(decision_at=NOW + timedelta(seconds=60))["score"] == 60
    assert bound(received_at=NOW.replace(tzinfo=None))["score"] is None


def test_hash_required():
    assert (
        catalog_liquidity(MARKET, catalog_sha256="bad", received_at=NOW, decision_at=NOW)["score"]
        is None
    )


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_score_wiring_preserves_book_gates(side):
    book = {"orderbook": {"yes_dollars": [["0.49", "3"]], "no_dollars": [["0.50", "3"]]}}
    assert not usable_bid_ask_book(book, side=side).usable
    assert usable_bid_ask_book(book, side=side, liquidity_score=bound()["score"]).usable
    assert not usable_bid_ask_book(book, side=side, liquidity_score=Decimal(29)).usable
    thin = {"orderbook": {"yes_dollars": [["0.49", "0.72"]], "no_dollars": [["0.50", "3"]]}}
    assert not usable_bid_ask_book(thin, side=side, liquidity_score=60).usable
    wide = {"orderbook": {"yes_dollars": [["0.40", "3"]], "no_dollars": [["0.50", "3"]]}}
    assert not usable_bid_ask_book(wide, side=side, liquidity_score=60).usable
