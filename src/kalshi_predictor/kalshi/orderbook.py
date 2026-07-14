from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import ONE_DOLLAR, to_decimal

ZERO = Decimal("0")


@dataclass(frozen=True)
class BestPrices:
    best_yes_bid: Decimal | None
    best_no_bid: Decimal | None
    best_yes_ask: Decimal | None
    best_no_ask: Decimal | None
    spread: Decimal | None


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class UsableBidAskBook:
    side: str
    usable: bool
    state: str
    reason: str
    bid_price: Decimal | None
    bid_depth: Decimal | None
    ask_price: Decimal | None
    ask_depth: Decimal | None
    spread: Decimal | None
    liquidity_score: Decimal | None
    min_depth: Decimal
    min_liquidity_score: Decimal
    max_spread: Decimal

    @property
    def has_visible_bid_ask(self) -> bool:
        return self.bid_price is not None and self.ask_price is not None

    @property
    def has_executable_depth(self) -> bool:
        return (
            self.bid_depth is not None
            and self.ask_depth is not None
            and self.bid_depth >= self.min_depth
            and self.ask_depth >= self.min_depth
        )


def parse_orderbook(orderbook_json: dict[str, Any] | None) -> BestPrices:
    if not orderbook_json:
        return _empty_prices()

    orderbook = _find_orderbook_container(orderbook_json)
    yes_levels = orderbook.get("yes_dollars")
    no_levels = orderbook.get("no_dollars")
    cents_fallback = False

    if yes_levels is None and no_levels is None:
        yes_levels = orderbook.get("yes")
        no_levels = orderbook.get("no")
        cents_fallback = True

    best_yes_bid = _best_bid(yes_levels, cents=cents_fallback)
    best_no_bid = _best_bid(no_levels, cents=cents_fallback)

    best_yes_ask = ONE_DOLLAR - best_no_bid if best_no_bid is not None else None
    best_no_ask = ONE_DOLLAR - best_yes_bid if best_yes_bid is not None else None
    spread = (
        best_yes_ask - best_yes_bid
        if best_yes_ask is not None and best_yes_bid is not None
        else None
    )

    return BestPrices(
        best_yes_bid=best_yes_bid,
        best_no_bid=best_no_bid,
        best_yes_ask=best_yes_ask,
        best_no_ask=best_no_ask,
        spread=spread,
    )


def usable_bid_ask_book(
    orderbook_json: dict[str, Any] | None,
    *,
    side: str,
    liquidity_score: Any = None,
    min_liquidity_score: Decimal = Decimal("30"),
    max_spread: Decimal = Decimal("0.02"),
    min_depth: Decimal = Decimal("1"),
) -> UsableBidAskBook:
    """Return whether the visible Kalshi book is safe to treat as executable.

    Kalshi binary asks are implied from the opposite side's best bid. A buy-YES
    ask therefore needs visible NO bid depth, while a buy-NO ask needs visible
    YES bid depth. This helper keeps that rule in one place for paper gates.
    """
    normalized_side = _normalize_contract_side(side)
    liquidity = to_decimal(liquidity_score)
    orderbook = _find_orderbook_container(orderbook_json or {})
    cents_fallback = not (
        "yes_dollars" in orderbook or "no_dollars" in orderbook
    )
    yes_levels = _book_levels(
        orderbook.get("yes_dollars", orderbook.get("yes")),
        cents=cents_fallback,
    )
    no_levels = _book_levels(
        orderbook.get("no_dollars", orderbook.get("no")),
        cents=cents_fallback,
    )
    yes_best = _best_level(yes_levels)
    no_best = _best_level(no_levels)

    if normalized_side == "NO":
        bid_level = no_best
        ask_source = yes_best
        bid_price = bid_level.price if bid_level is not None else None
        bid_depth = bid_level.quantity if bid_level is not None else None
        ask_price = ONE_DOLLAR - ask_source.price if ask_source is not None else None
        ask_depth = ask_source.quantity if ask_source is not None else None
    else:
        bid_level = yes_best
        ask_source = no_best
        bid_price = bid_level.price if bid_level is not None else None
        bid_depth = bid_level.quantity if bid_level is not None else None
        ask_price = ONE_DOLLAR - ask_source.price if ask_source is not None else None
        ask_depth = ask_source.quantity if ask_source is not None else None

    spread = ask_price - bid_price if bid_price is not None and ask_price is not None else None
    result = UsableBidAskBook(
        side=normalized_side,
        usable=False,
        state="NO_EXECUTABLE_BOOK",
        reason="Missing visible bid/ask levels.",
        bid_price=bid_price,
        bid_depth=bid_depth,
        ask_price=ask_price,
        ask_depth=ask_depth,
        spread=spread,
        liquidity_score=liquidity,
        min_depth=min_depth,
        min_liquidity_score=min_liquidity_score,
        max_spread=max_spread,
    )
    return _classify_usable_book(result)


def _empty_prices() -> BestPrices:
    return BestPrices(
        best_yes_bid=None,
        best_no_bid=None,
        best_yes_ask=None,
        best_no_ask=None,
        spread=None,
    )


def _find_orderbook_container(orderbook_json: dict[str, Any]) -> dict[str, Any]:
    for key in ("orderbook_fp", "orderbook"):
        value = orderbook_json.get(key)
        if isinstance(value, dict):
            return value
    return orderbook_json


def _normalize_contract_side(side: str) -> str:
    normalized = str(side or "").upper()
    return "NO" if normalized.endswith("NO") else "YES"


def _best_bid(levels: Any, *, cents: bool) -> Decimal | None:
    if not isinstance(levels, list):
        return None

    prices = [_extract_price(level, cents=cents) for level in levels]
    usable_prices = [price for price in prices if price is not None]
    return max(usable_prices) if usable_prices else None


def _extract_price(level: Any, *, cents: bool) -> Decimal | None:
    raw_price: Any
    if isinstance(level, dict):
        raw_price = (
            level.get("price_dollars")
            or level.get("yes_dollars")
            or level.get("no_dollars")
            or level.get("price")
            or level.get("yes")
            or level.get("no")
        )
    elif isinstance(level, (list, tuple)) and level:
        raw_price = level[0]
    else:
        raw_price = level

    price = to_decimal(raw_price)
    if price is None:
        return None
    if cents and price > ONE_DOLLAR:
        return price / Decimal("100")
    return price


def _book_levels(levels: Any, *, cents: bool) -> list[BookLevel]:
    if not isinstance(levels, list):
        return []
    parsed = [_extract_level(level, cents=cents) for level in levels]
    return [level for level in parsed if level is not None]


def _best_level(levels: list[BookLevel]) -> BookLevel | None:
    if not levels:
        return None
    best_price = max(level.price for level in levels)
    best_quantity = sum(
        (level.quantity for level in levels if level.price == best_price),
        ZERO,
    )
    return BookLevel(price=best_price, quantity=best_quantity)


def _extract_level(level: Any, *, cents: bool) -> BookLevel | None:
    raw_quantity: Any
    if isinstance(level, dict):
        raw_quantity = (
            level.get("quantity")
            or level.get("size")
            or level.get("contracts")
            or level.get("count")
        )
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        raw_quantity = level[1]
    else:
        raw_quantity = None

    price = _extract_price(level, cents=cents)
    quantity = to_decimal(raw_quantity)
    if price is None or quantity is None or quantity <= ZERO:
        return None
    return BookLevel(price=price, quantity=quantity)


def _classify_usable_book(book: UsableBidAskBook) -> UsableBidAskBook:
    state = "CLEAN_BOOK"
    reason = "Visible bid/ask depth, liquidity, and spread are executable."
    usable = True

    if not book.has_visible_bid_ask:
        state = "NO_EXECUTABLE_BOOK"
        reason = "Missing visible bid/ask levels."
        usable = False
    elif book.spread is None or book.spread < ZERO:
        state = "NO_EXECUTABLE_BOOK"
        reason = "Visible book is crossed or invalid."
        usable = False
    elif not book.has_executable_depth:
        state = "NO_EXECUTABLE_BOOK"
        reason = (
            "Visible bid/ask levels do not have enough executable depth at the "
            "best prices."
        )
        usable = False
    elif book.liquidity_score is None or book.liquidity_score <= ZERO:
        state = "NO_EXECUTABLE_BOOK"
        reason = "No positive visible liquidity score is available."
        usable = False
    elif book.liquidity_score < book.min_liquidity_score:
        state = "THIN_BOOK"
        reason = "Liquidity score is below the executable threshold."
        usable = False
    elif book.spread > book.max_spread:
        state = "WIDE_SPREAD"
        reason = "Bid/ask spread is wider than the executable threshold."
        usable = False

    return UsableBidAskBook(
        side=book.side,
        usable=usable,
        state=state,
        reason=reason,
        bid_price=book.bid_price,
        bid_depth=book.bid_depth,
        ask_price=book.ask_price,
        ask_depth=book.ask_depth,
        spread=book.spread,
        liquidity_score=book.liquidity_score,
        min_depth=book.min_depth,
        min_liquidity_score=book.min_liquidity_score,
        max_spread=book.max_spread,
    )
