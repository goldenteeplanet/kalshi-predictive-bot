import json

import pytest

from kalshi_predictor.research.event_quote_collector import (
    EventCandidate,
    executable_yes_bounds,
    liquidity_coverage,
)


def test_sparse_quotes_become_bounds_not_midpoints():
    candidate = EventCandidate("EVENT", "KXBTC", "BTC", ())
    coverage = liquidity_coverage(
        candidate,
        [
            {"ticker": "A", "yes_bid": "0.20", "no_bid": "0.70"},
            {"ticker": "B", "yes_bid": None, "no_bid": "0.20"},
        ],
        coherence_ms=100,
    )
    bounds = json.loads(coverage.bounds_json)
    assert coverage.two_sided_count == 1
    assert coverage.ask_only_count == 1
    assert coverage.two_sided_coverage == "0.50000000"
    assert bounds["buckets"][1] == {
        "ticker": "B",
        "kind": None,
        "has_bid": False,
        "has_ask": True,
        "yes_lower_source": "MISSING",
        "yes_upper_source": "NO_BID_COMPLEMENT",
        "lower": 0.0,
        "upper": 0.8,
    }


def test_complete_vector_is_marked_executable():
    candidate = EventCandidate("EVENT", "KXETH", "ETH", ())
    coverage = liquidity_coverage(
        candidate,
        [
            {"ticker": "A", "yes_bid": "0.30", "no_bid": "0.60"},
            {"ticker": "B", "yes_bid": "0.60", "no_bid": "0.30"},
        ],
        coherence_ms=50,
    )
    assert coverage.complete_executable == "true"
    assert coverage.bounds_feasible == "true"


def test_no_bid_becomes_yes_upper_bound_without_inventing_a_lower_bound():
    converted = executable_yes_bounds(
        {"yes_bid": None, "no_bid": "0.82", "no_ask": "0.41"}
    )
    assert converted["bid"] is None
    assert converted["ask"] == pytest.approx(0.18)
    assert converted["bid_source"] == "MISSING"
    assert converted["ask_source"] == "NO_BID_COMPLEMENT"
