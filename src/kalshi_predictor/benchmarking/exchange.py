from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from kalshi_predictor.benchmarking.agents import OrderIntent
from kalshi_predictor.kalshi.orderbook import LocalOrderbook


@dataclass(frozen=True)
class LimitOrder:
    order_id: str
    ticker: str
    outcome: str
    action: str
    size: Decimal
    limit_price: Decimal
    time_in_force: str
    queue_ahead: Decimal = Decimal("0")
    filled_size: Decimal = Decimal("0")
    status: str = "NEW"


@dataclass(frozen=True)
class OrderEvent:
    order_id: str
    event: str
    filled_size: Decimal
    price: Decimal | None
    remaining_size: Decimal
    reason: str


class ReplayExchange:
    def __init__(self) -> None:
        self.resting: dict[str, LimitOrder] = {}
        self.events: list[OrderEvent] = []

    def submit(self, order: LimitOrder, book: LocalOrderbook) -> OrderEvent:
        tif = order.time_in_force.upper()
        if tif not in {"IOC", "GTC", "POST_ONLY"}:
            raise ValueError("time_in_force must be IOC, GTC, or POST_ONLY")
        quote = book.execution_quote(
            outcome=order.outcome, action=order.action, size=order.size
        )
        crosses = (
            quote.average_price is not None
            and _within_limit(order.action, quote.average_price, order.limit_price)
        )
        if tif == "POST_ONLY" and crosses:
            return self._record(order, "REJECTED", Decimal("0"), None,
                                "POST_ONLY_WOULD_CROSS")
        filled = quote.filled_size if crosses else Decimal("0")
        remaining = order.size - filled
        if tif == "IOC":
            return self._record(
                order, "FILLED" if remaining == 0 else "CANCELLED",
                filled, quote.average_price if filled else None, "IOC_REMAINDER_CANCELLED",
            )
        if remaining > 0:
            queue = _visible_queue(book, order)
            self.resting[order.order_id] = replace(
                order, size=remaining, queue_ahead=queue, filled_size=filled, status="RESTING"
            )
            return self._record(order, "RESTING", filled,
                                quote.average_price if filled else None, "RESTING_AT_LIMIT")
        return self._record(order, "FILLED", filled, quote.average_price, "FULLY_EXECUTED")

    def process_trade(self, *, ticker: str, outcome: str, price: Decimal,
                      size: Decimal) -> list[OrderEvent]:
        emitted = []
        remaining_trade = size
        for order_id in sorted(self.resting):
            order = self.resting[order_id]
            if order.ticker != ticker or order.outcome != outcome or order.limit_price != price:
                continue
            queue_consumed = min(order.queue_ahead, remaining_trade)
            remaining_trade -= queue_consumed
            queue_left = order.queue_ahead - queue_consumed
            fill = min(order.size, remaining_trade) if queue_left == 0 else Decimal("0")
            remaining_trade -= fill
            updated = replace(
                order, size=order.size - fill, queue_ahead=queue_left,
                filled_size=order.filled_size + fill,
                status="FILLED" if order.size == fill else "RESTING",
            )
            if updated.size == 0:
                del self.resting[order_id]
            else:
                self.resting[order_id] = updated
            if fill:
                emitted.append(self._record(updated, updated.status, fill, price,
                                            "DETERMINISTIC_QUEUE_FILL"))
            if remaining_trade == 0:
                break
        return emitted

    def cancel(self, order_id: str) -> OrderEvent:
        order = self.resting.pop(order_id)
        return self._record(order, "CANCELLED", Decimal("0"), None, "OPERATOR_CANCEL")

    def replace(self, order_id: str, replacement: LimitOrder,
                book: LocalOrderbook) -> tuple[OrderEvent, OrderEvent]:
        cancelled = self.cancel(order_id)
        submitted = self.submit(replacement, book)
        return cancelled, submitted

    def _record(self, order: LimitOrder, event: str, filled: Decimal,
                price: Decimal | None, reason: str) -> OrderEvent:
        row = OrderEvent(order.order_id, event, filled, price,
                         max(Decimal("0"), order.size - filled), reason)
        self.events.append(row)
        return row


def _within_limit(action: str, price: Decimal, limit: Decimal) -> bool:
    return price <= limit if action.lower() == "buy" else price >= limit


def _visible_queue(book: LocalOrderbook, order: LimitOrder) -> Decimal:
    levels = book.yes if order.outcome == "yes" else book.no
    return levels.get(order.limit_price, Decimal("0"))


def intent_as_ioc(intent: OrderIntent, limit_price: Decimal, order_id: str) -> LimitOrder:
    return LimitOrder(order_id, intent.ticker, intent.outcome, intent.action,
                      intent.size, limit_price, "IOC")
