from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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


class OrderbookProtocolError(ValueError):
    """Raised when an orderbook message cannot be applied safely."""


class OrderbookSequenceGap(OrderbookProtocolError):
    def __init__(self, *, ticker: str, expected: int, actual: int) -> None:
        super().__init__(f"Orderbook sequence gap for {ticker}: expected {expected}, got {actual}.")
        self.ticker = ticker
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class ExecutionQuote:
    outcome: str
    action: str
    requested_size: Decimal
    filled_size: Decimal
    total_value: Decimal
    average_price: Decimal | None
    fully_executable: bool


@dataclass
class LocalOrderbook:
    """Read-only local Kalshi book reconstructed from snapshots and deltas."""

    ticker: str
    yes: dict[Decimal, Decimal] = field(default_factory=dict)
    no: dict[Decimal, Decimal] = field(default_factory=dict)
    sid: int | None = None
    sequence: int | None = None
    recovery_count: int = 0

    def apply_snapshot(self, message: Mapping[str, Any]) -> None:
        payload = _message_payload(message)
        ticker = str(payload.get("market_ticker") or self.ticker)
        if ticker != self.ticker:
            raise OrderbookProtocolError(f"Snapshot ticker {ticker} does not match {self.ticker}.")
        self.yes = _levels(payload, "yes_dollars_fp", "yes_dollars")
        self.no = _levels(payload, "no_dollars_fp", "no_dollars")
        self.sid = _optional_int(message.get("sid"))
        self.sequence = _optional_int(message.get("seq"))

    def apply_rest_snapshot(
        self,
        orderbook: Mapping[str, Any],
        *,
        resume_sequence: int,
        sid: int | None = None,
    ) -> None:
        payload = orderbook.get("orderbook_fp")
        if not isinstance(payload, Mapping):
            payload = orderbook
        self.yes = _levels(payload, "yes_dollars", "yes_dollars_fp")
        self.no = _levels(payload, "no_dollars", "no_dollars_fp")
        self.sequence = resume_sequence
        if sid is not None:
            self.sid = sid
        self.recovery_count += 1

    def apply_delta(self, message: Mapping[str, Any]) -> None:
        payload = _message_payload(message)
        ticker = str(payload.get("market_ticker") or "")
        if ticker != self.ticker:
            raise OrderbookProtocolError(f"Delta ticker {ticker} does not match {self.ticker}.")
        actual = _required_int(message.get("seq"), "seq")
        if self.sequence is None:
            raise OrderbookProtocolError(f"Delta for {self.ticker} arrived before a snapshot.")
        expected = self.sequence + 1
        if actual != expected:
            raise OrderbookSequenceGap(ticker=self.ticker, expected=expected, actual=actual)
        side = str(payload.get("side") or "").lower()
        if side not in {"yes", "no"}:
            raise OrderbookProtocolError(f"Unsupported orderbook side: {side!r}.")
        price = _decimal(payload.get("price_dollars"), "price_dollars")
        delta = _decimal(payload.get("delta_fp"), "delta_fp")
        levels = self.yes if side == "yes" else self.no
        quantity = levels.get(price, Decimal(0)) + delta
        if quantity <= 0:
            levels.pop(price, None)
        else:
            levels[price] = quantity
        self.sequence = actual
        message_sid = _optional_int(message.get("sid"))
        if message_sid is not None:
            self.sid = message_sid

    @property
    def best_yes_bid(self) -> Decimal | None:
        return max(self.yes, default=None)

    @property
    def best_yes_ask(self) -> Decimal | None:
        best_no_bid = max(self.no, default=None)
        return Decimal(1) - best_no_bid if best_no_bid is not None else None

    @property
    def best_no_bid(self) -> Decimal | None:
        return max(self.no, default=None)

    @property
    def best_no_ask(self) -> Decimal | None:
        best_yes_bid = max(self.yes, default=None)
        return Decimal(1) - best_yes_bid if best_yes_bid is not None else None

    @property
    def spread(self) -> Decimal | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return self.best_yes_ask - self.best_yes_bid

    @property
    def midpoint(self) -> Decimal | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return (self.best_yes_bid + self.best_yes_ask) / 2

    def depth(self, *, side: str, levels: int = 5) -> Decimal:
        book = self.yes if side.lower() == "yes" else self.no
        prices = sorted(book, reverse=True)[: max(0, levels)]
        return sum((book[price] for price in prices), Decimal(0))

    @property
    def imbalance(self) -> Decimal | None:
        yes_depth = sum(self.yes.values(), Decimal(0))
        no_depth = sum(self.no.values(), Decimal(0))
        total = yes_depth + no_depth
        return (yes_depth - no_depth) / total if total else None

    def execution_quote(self, *, outcome: str, action: str, size: Decimal | str) -> ExecutionQuote:
        resolved_outcome = outcome.lower()
        resolved_action = action.lower()
        requested = _decimal(size, "size")
        if requested <= 0:
            raise OrderbookProtocolError("Execution quote size must be positive.")
        if resolved_outcome not in {"yes", "no"} or resolved_action not in {"buy", "sell"}:
            raise OrderbookProtocolError("Execution quote requires yes/no and buy/sell.")

        if (resolved_outcome, resolved_action) == ("yes", "buy"):
            levels = [(Decimal(1) - price, qty) for price, qty in self.no.items()]
        elif (resolved_outcome, resolved_action) == ("yes", "sell"):
            levels = list(self.yes.items())
        elif (resolved_outcome, resolved_action) == ("no", "buy"):
            levels = [(Decimal(1) - price, qty) for price, qty in self.yes.items()]
        else:
            levels = list(self.no.items())
        levels.sort(key=lambda item: item[0], reverse=resolved_action == "sell")

        remaining = requested
        filled = Decimal(0)
        total = Decimal(0)
        for price, quantity in levels:
            take = min(remaining, quantity)
            total += take * price
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        return ExecutionQuote(
            outcome=resolved_outcome,
            action=resolved_action,
            requested_size=requested,
            filled_size=filled,
            total_value=total,
            average_price=(total / filled if filled else None),
            fully_executable=filled == requested,
        )

    def as_orderbook_json(self) -> dict[str, Any]:
        return {
            "orderbook_fp": {
                "yes_dollars": _serialized_levels(self.yes),
                "no_dollars": _serialized_levels(self.no),
            },
            "gh1_local_orderbook": {
                "ticker": self.ticker,
                "sid": self.sid,
                "sequence": self.sequence,
                "recovery_count": self.recovery_count,
                "spread": _string(self.spread),
                "midpoint": _string(self.midpoint),
                "imbalance": _string(self.imbalance),
                "yes_depth_5": _string(self.depth(side="yes", levels=5)),
                "no_depth_5": _string(self.depth(side="no", levels=5)),
                "read_only": True,
                "execution_enabled": False,
            },
        }


def _message_payload(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("msg")
    if not isinstance(payload, Mapping):
        raise OrderbookProtocolError("Orderbook message is missing an object msg payload.")
    return payload


def _levels(payload: Mapping[str, Any], *keys: str) -> dict[Decimal, Decimal]:
    raw: Any = None
    for key in keys:
        if key in payload:
            raw = payload[key]
            break
    if raw is None:
        return {}
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise OrderbookProtocolError("Orderbook levels must be an array.")
    result: dict[Decimal, Decimal] = {}
    for row in raw:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 2:
            raise OrderbookProtocolError("Each orderbook level must contain price and quantity.")
        price = _decimal(row[0], "price")
        quantity = _decimal(row[1], "quantity")
        if quantity > 0:
            result[price] = quantity
    return result


def _serialized_levels(levels: Mapping[Decimal, Decimal]) -> list[list[str]]:
    return [[str(price), str(levels[price])] for price in sorted(levels)]


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise OrderbookProtocolError(f"Invalid decimal {field_name}: {value!r}.") from exc


def _required_int(value: Any, field_name: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise OrderbookProtocolError(f"Missing integer {field_name}.")
    return parsed


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise OrderbookProtocolError(f"Invalid integer value: {value!r}.") from exc


def _string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


# Existing REST snapshot compatibility API. These functions intentionally stay
# separate from LocalOrderbook so established forecast/risk callers keep their
# prior behavior.
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
    return BestPrices(best_yes_bid, best_no_bid, best_yes_ask, best_no_ask, spread)


def usable_bid_ask_book(
    orderbook_json: dict[str, Any] | None,
    *,
    side: str,
    liquidity_score: Any = None,
    min_liquidity_score: Decimal = Decimal("30"),
    max_spread: Decimal = Decimal("0.02"),
    min_depth: Decimal = Decimal("1"),
) -> UsableBidAskBook:
    normalized_side = _normalize_contract_side(side)
    liquidity = to_decimal(liquidity_score)
    orderbook = _find_orderbook_container(orderbook_json or {})
    cents_fallback = not ("yes_dollars" in orderbook or "no_dollars" in orderbook)
    yes_levels = _book_levels(
        orderbook.get("yes_dollars", orderbook.get("yes")), cents=cents_fallback
    )
    no_levels = _book_levels(orderbook.get("no_dollars", orderbook.get("no")), cents=cents_fallback)
    yes_best = _best_level(yes_levels)
    no_best = _best_level(no_levels)
    if normalized_side == "NO":
        bid_level, ask_source = no_best, yes_best
    else:
        bid_level, ask_source = yes_best, no_best
    bid_price = bid_level.price if bid_level is not None else None
    bid_depth = bid_level.quantity if bid_level is not None else None
    ask_price = ONE_DOLLAR - ask_source.price if ask_source is not None else None
    ask_depth = ask_source.quantity if ask_source is not None else None
    spread = ask_price - bid_price if bid_price is not None and ask_price is not None else None
    return _classify_usable_book(
        UsableBidAskBook(
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
    )


def _empty_prices() -> BestPrices:
    return BestPrices(None, None, None, None, None)


def _find_orderbook_container(orderbook_json: dict[str, Any]) -> dict[str, Any]:
    for key in ("orderbook_fp", "orderbook"):
        value = orderbook_json.get(key)
        if isinstance(value, dict):
            return value
    return orderbook_json


def _normalize_contract_side(side: str) -> str:
    return "NO" if str(side or "").upper().endswith("NO") else "YES"


def _best_bid(levels: Any, *, cents: bool) -> Decimal | None:
    if not isinstance(levels, list):
        return None
    prices = [_extract_price(level, cents=cents) for level in levels]
    usable_prices = [price for price in prices if price is not None]
    return max(usable_prices) if usable_prices else None


def _extract_price(level: Any, *, cents: bool) -> Decimal | None:
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
    return price / Decimal("100") if cents and price > ONE_DOLLAR else price


def _book_levels(levels: Any, *, cents: bool) -> list[BookLevel]:
    if not isinstance(levels, list):
        return []
    parsed = [_extract_level(level, cents=cents) for level in levels]
    return [level for level in parsed if level is not None]


def _best_level(levels: list[BookLevel]) -> BookLevel | None:
    if not levels:
        return None
    best_price = max(level.price for level in levels)
    best_quantity = sum((level.quantity for level in levels if level.price == best_price), ZERO)
    return BookLevel(best_price, best_quantity)


def _extract_level(level: Any, *, cents: bool) -> BookLevel | None:
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
    return BookLevel(price, quantity)


def _classify_usable_book(book: UsableBidAskBook) -> UsableBidAskBook:
    state, reason, usable = (
        "CLEAN_BOOK",
        "Visible bid/ask depth, liquidity, and spread are executable.",
        True,
    )
    if not book.has_visible_bid_ask:
        state, reason, usable = "NO_EXECUTABLE_BOOK", "Missing visible bid/ask levels.", False
    elif book.spread is None or book.spread < ZERO:
        state, reason, usable = "NO_EXECUTABLE_BOOK", "Visible book is crossed or invalid.", False
    elif not book.has_executable_depth:
        state, reason, usable = (
            "NO_EXECUTABLE_BOOK",
            "Visible bid/ask levels do not have enough executable depth at the best prices.",
            False,
        )
    elif book.liquidity_score is None or book.liquidity_score <= ZERO:
        state, reason, usable = (
            "NO_EXECUTABLE_BOOK",
            "No positive visible liquidity score is available.",
            False,
        )
    elif book.liquidity_score < book.min_liquidity_score:
        state, reason, usable = (
            "THIN_BOOK",
            "Liquidity score is below the executable threshold.",
            False,
        )
    elif book.spread > book.max_spread:
        state, reason, usable = (
            "WIDE_SPREAD",
            "Bid/ask spread is wider than the executable threshold.",
            False,
        )
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
