from decimal import Decimal

import pytest

from kalshi_predictor.microstructure.orderbook_features import parse_orderbook_depth


@pytest.mark.parametrize(
    "book",
    [
        {
            "orderbook_fp": {
                "yes_dollars": [["0.50", "2"], ["0.40", "100"]],
                "no_dollars": [["0.45", "3"]],
            }
        },
        {"orderbook": {"yes": [[40, 100], [50, 2]], "no": [[45, 3]]}},
        {
            "yes_dollars": [
                {"price": "0.40", "quantity": 100},
                {"price_dollars": "0.50", "quantity": 2},
            ],
            "no_dollars": [["0.45", "3"]],
        },
    ],
)
def test_top_depth_excludes_deeper_levels(book: dict) -> None:
    depth = parse_orderbook_depth(book)
    assert depth["top_of_book_depth"] == Decimal("5")
    assert depth["total_depth"] == Decimal("105")
    assert depth["yes_bid_depth"] == Decimal("102")


def test_top_depth_combines_equal_best_levels_and_ignores_zero_quote() -> None:
    depth = parse_orderbook_depth({"yes": [[60, 0], [50, 2], [40, 100], [50, 3]]})
    assert depth["top_of_book_depth"] == Decimal("5")
    assert depth["total_depth"] == Decimal("105")


def test_empty_top_depth_retains_zero() -> None:
    assert parse_orderbook_depth(None)["top_of_book_depth"] == Decimal("0")
