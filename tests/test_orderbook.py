from decimal import Decimal

from kalshi_predictor.kalshi.orderbook import parse_orderbook, usable_bid_ask_book


def test_parse_orderbook_derives_implied_asks() -> None:
    orderbook = {
        "orderbook_fp": {
            "yes_dollars": [["0.32", "12"], ["0.35", "2"]],
            "no_dollars": [["0.63", "1"], ["0.62", "5"]],
        }
    }

    prices = parse_orderbook(orderbook)

    assert prices.best_yes_bid == Decimal("0.35")
    assert prices.best_no_bid == Decimal("0.63")
    assert prices.best_yes_ask == Decimal("0.37")
    assert prices.best_no_ask == Decimal("0.65")
    assert prices.spread == Decimal("0.02")


def test_parse_orderbook_handles_empty_book() -> None:
    prices = parse_orderbook({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})

    assert prices.best_yes_bid is None
    assert prices.best_no_bid is None
    assert prices.best_yes_ask is None
    assert prices.best_no_ask is None
    assert prices.spread is None


def test_usable_bid_ask_book_uses_opposite_side_depth_for_buy_yes() -> None:
    book = usable_bid_ask_book(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.38", "4"]],
                "no_dollars": [["0.59", "3"]],
            }
        },
        side="BUY_YES",
        liquidity_score="80",
        max_spread=Decimal("0.05"),
    )

    assert book.usable is True
    assert book.state == "CLEAN_BOOK"
    assert book.bid_price == Decimal("0.38")
    assert book.bid_depth == Decimal("4")
    assert book.ask_price == Decimal("0.41")
    assert book.ask_depth == Decimal("3")
    assert book.spread == Decimal("0.03")


def test_usable_bid_ask_book_uses_opposite_side_depth_for_buy_no() -> None:
    book = usable_bid_ask_book(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.38", "4"]],
                "no_dollars": [["0.59", "3"]],
            }
        },
        side="BUY_NO",
        liquidity_score="80",
        max_spread=Decimal("0.05"),
    )

    assert book.usable is True
    assert book.state == "CLEAN_BOOK"
    assert book.bid_price == Decimal("0.59")
    assert book.bid_depth == Decimal("3")
    assert book.ask_price == Decimal("0.62")
    assert book.ask_depth == Decimal("4")
    assert book.spread == Decimal("0.03")


def test_usable_bid_ask_book_blocks_missing_executable_depth() -> None:
    book = usable_bid_ask_book(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.38", "4"]],
                "no_dollars": [["0.59", "0"]],
            }
        },
        side="BUY_YES",
        liquidity_score="80",
        max_spread=Decimal("0.05"),
    )

    assert book.usable is False
    assert book.state == "NO_EXECUTABLE_BOOK"
    assert book.ask_price is None
    assert book.ask_depth is None


def test_usable_bid_ask_book_blocks_thin_liquidity_and_wide_spread() -> None:
    orderbook = {
        "orderbook_fp": {
            "yes_dollars": [["0.38", "4"]],
            "no_dollars": [["0.59", "3"]],
        }
    }

    thin = usable_bid_ask_book(
        orderbook,
        side="BUY_YES",
        liquidity_score="5",
        max_spread=Decimal("0.05"),
    )
    wide = usable_bid_ask_book(
        orderbook,
        side="BUY_YES",
        liquidity_score="80",
        max_spread=Decimal("0.01"),
    )

    assert thin.usable is False
    assert thin.state == "THIN_BOOK"
    assert wide.usable is False
    assert wide.state == "WIDE_SPREAD"
