from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.overnight_paper.books import qualify_book

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def probe(yes, no, **kwargs):
    return qualify_book(
        {"orderbook_fp": {"yes_dollars": yes, "no_dollars": no}},
        received_at=kwargs.pop("received_at", NOW),
        now=NOW,
        max_spread=Decimal("0.10"),
        liquidity_score=Decimal("50"),
        price_ranges=[{"start": "0", "end": "1", "step": "0.01"}],
        **kwargs,
    )


def test_reciprocity_and_exact_visible_price():
    result = probe([["0.41", "3"]], [["0.55", "7"]])
    assert result["yes_ask"] == "0.45"
    assert result["no_ask"] == "0.59"
    assert result["sides"]["YES"]["ask_depth"] == "7"
    assert result["executable"]


def test_missing_no_never_invents_yes_ask():
    result = probe([["0.41", "3"]], [])
    assert result["yes_ask"] is None
    assert result["no_ask"] == "0.59"
    assert not result["executable"]  # Preserve canonical reciprocal/spread requirement.


@pytest.mark.parametrize(
    "yes,no",
    [
        ([["0.6", "1"]], [["0.6", "1"]]),
        ([["0.4", "0.5"]], [["0.55", "1"]]),
        ([["0.401", "5"]], [["0.55", "1"]]),
        ([["NaN", "5"]], [["0.55", "1"]]),
        ([["0.4", "-1"]], [["0.55", "1"]]),
        ([["0.4", "1"], ["0.4", "2"]], [["0.55", "1"]]),
    ],
)
def test_invalid_or_insufficient_books_fail_closed(yes, no):
    assert not probe(yes, no)["executable"]


@pytest.mark.parametrize("offset", [-1, 901])
def test_future_or_stale_receipt_rejected(offset):
    assert not probe([["0.41", "3"]], [["0.55", "7"]], received_at=NOW - timedelta(seconds=offset))[
        "executable"
    ]
