"""Offline market-microstructure replay with pessimistic fill envelopes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

SCHEMA = "phase4nd.microstructure-replay.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def replay_order(events: object, order: object, *, outcome: str) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(events, list) or not isinstance(order, dict):
        return _result(["INPUT_INVALID"], {}, order, outcome)
    decision = _time(order.get("decision_time"))
    if decision is None:
        return _result(["DECISION_TIME_INVALID"], {}, order, outcome)
    arrival = decision + timedelta(milliseconds=int(order.get("latency_ms", 0)))
    acknowledged = arrival + timedelta(milliseconds=int(order.get("ack_delay_ms", 0)))
    previous_time = None
    seen_sequences: set[int] = set()
    normalized = []
    for index, event in enumerate(events):
        timestamp = _time(event.get("timestamp")) if isinstance(event, dict) else None
        sequence = event.get("sequence") if isinstance(event, dict) else None
        if timestamp is None or type(sequence) is not int:
            errors.append(f"EVENT_{index}_SHAPE_INVALID")
            continue
        if sequence in seen_sequences or sequence != index + 1:
            errors.append(f"EVENT_{index}_MISSING_OR_DUPLICATE_SEQUENCE")
        if previous_time is not None and timestamp < previous_time:
            errors.append(f"EVENT_{index}_TIME_REVERSED")
        seen_sequences.add(sequence)
        previous_time = timestamp
        normalized.append({**event, "_time": timestamp})
    for left, right in zip(normalized, normalized[1:], strict=False):
        if left["_time"] == right["_time"] and left["sequence"] == right["sequence"]:
            errors.append("SAME_TIMESTAMP_ORDERING_AMBIGUOUS")
    books = [
        event for event in normalized if event.get("type") == "BOOK" and event["_time"] <= arrival
    ]
    if not books:
        errors.append("NO_BOOK_AT_ARRIVAL")
        return _result(sorted(set(errors)), {}, order, outcome)
    book = books[-1]
    age_ms = int((arrival - book["_time"]).total_seconds() * 1000)
    if age_ms > int(order.get("max_book_age_ms", 0)):
        errors.append("STALE_BOOK")
    bid, ask = Decimal(str(book.get("bid"))), Decimal(str(book.get("ask")))
    tick = Decimal(str(order.get("tick_size", "0.01")))
    limit = Decimal(str(order.get("limit_price")))
    if bid >= ask:
        errors.append("CROSSED_BOOK")
    if limit % tick != 0:
        errors.append("PRICE_NOT_ON_TICK")
    if book.get("status") != "open" or any(
        event.get("type") == "CLOSE" and event["_time"] <= acknowledged for event in normalized
    ):
        errors.append("MARKET_CLOSED")
    if order.get("hidden_liquidity") not in {None, 0, "0"}:
        errors.append("HIDDEN_LIQUIDITY_ASSUMPTION_FORBIDDEN")
    if errors:
        return _result(sorted(set(errors)), {}, order, outcome)
    quantity = Decimal(str(order.get("quantity")))
    fee = Decimal(str(order.get("fee_per_contract", "0")))
    side = str(order.get("side"))
    contra_price = ask if side == "yes" else Decimal("1") - bid
    contra_size = Decimal(str(book.get("ask_size" if side == "yes" else "bid_size", 0)))
    crosses = limit >= contra_price
    optimistic_fill = (
        quantity
        if crosses
        else _resting_fill(normalized, order, arrival, queue_multiplier=Decimal("0"))
    )
    central_fill = (
        min(quantity, contra_size)
        if crosses
        else _resting_fill(normalized, order, arrival, queue_multiplier=Decimal("1"))
    )
    pessimistic_fill = (
        min(
            quantity,
            max(Decimal("0"), contra_size - Decimal(str(order.get("burst_volume_ahead", 0)))),
        )
        if crosses and acknowledged <= arrival
        else _resting_fill(normalized, order, acknowledged, queue_multiplier=Decimal("1.5"))
    )
    if order.get("cancel_replace"):
        pessimistic_fill = _resting_fill(
            normalized, order, acknowledged, queue_multiplier=Decimal("2")
        )
    envelopes = {}
    for name, filled in (
        ("optimistic", optimistic_fill),
        ("central", central_fill),
        ("pessimistic", pessimistic_fill),
    ):
        fill_price = contra_price if crosses else limit
        envelopes[name] = _economics(filled, quantity, fill_price, fee, side, outcome)
    post_books = [
        event
        for event in normalized
        if event.get("type") == "BOOK" and event["_time"] > acknowledged
    ]
    if post_books and pessimistic_fill > 0:
        next_mid = (Decimal(str(post_books[0]["bid"])) + Decimal(str(post_books[0]["ask"]))) / 2
        entry = contra_price if crosses else limit
        adverse = entry - next_mid if side == "yes" else next_mid - (Decimal("1") - entry)
        envelopes["pessimistic"]["post_fill_adverse_selection"] = str(
            max(Decimal("0"), adverse) * pessimistic_fill
        )
    result = _result([], envelopes, order, outcome)
    result.update(
        {
            "arrival_time": arrival.isoformat(),
            "acknowledged_time": acknowledged.isoformat(),
            "book_sequence": book["sequence"],
            "book_age_ms": age_ms,
            "readiness_envelope": "pessimistic",
            "execution_capability": False,
        }
    )
    result["replay_sha256"] = _digest(result)
    return result


def _resting_fill(events, order, after, *, queue_multiplier):
    limit = Decimal(str(order["limit_price"]))
    quantity = Decimal(str(order["quantity"]))
    prior_books = [
        event for event in events if event.get("type") == "BOOK" and event["_time"] <= after
    ]
    book = prior_books[-1]
    side = str(order["side"])
    displayed_queue = Decimal(str(book.get("bid_size" if side == "yes" else "ask_size", 0)))
    queue = displayed_queue * queue_multiplier
    traded = Decimal("0")
    for event in events:
        if event["_time"] < after:
            continue
        if event.get("type") == "TRADE" and Decimal(str(event.get("price"))) == limit:
            traded += Decimal(str(event.get("size", 0)))
        if event.get("type") == "QUEUE_RESET":
            queue = max(queue, Decimal(str(event.get("queue_ahead", 0))))
    return min(quantity, max(Decimal("0"), traded - queue))


def _economics(filled, requested, price, fee, side, outcome):
    payout = filled if outcome == side else Decimal("0")
    gross = payout - price * filled
    fees = fee * filled
    return {
        "requested_quantity": str(requested),
        "filled_quantity": str(filled),
        "fill_rate": str(filled / requested if requested else 0),
        "fill_price": str(price),
        "gross_pnl": str(gross),
        "fees": str(fees),
        "net_pnl": str(gross - fees),
        "exposure": str(price * filled + fees),
    }


def compare_simple_fill(
    replay: dict[str, object],
    *,
    simple_price: str,
    quantity: int,
    fee_per_contract: str,
    side: str,
    outcome: str,
) -> dict[str, object]:
    simple = _economics(
        Decimal(quantity),
        Decimal(quantity),
        Decimal(simple_price),
        Decimal(fee_per_contract),
        side,
        outcome,
    )
    pessimistic = replay.get("envelopes", {}).get("pessimistic", {})
    inflation = Decimal(simple["net_pnl"]) - Decimal(str(pessimistic.get("net_pnl", 0)))
    result = {
        "simple_full_fill": simple,
        "pessimistic_replay": pessimistic,
        "simple_pnl_inflation": str(inflation),
        "readiness_uses_simple": False,
        "safety": _safety(),
    }
    result["comparison_sha256"] = _digest(result)
    return result


def _result(errors, envelopes, order, outcome):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "envelopes": envelopes,
        "order_source_unchanged": True,
        "outcome": outcome,
        "submitted_order": False,
        "safety": _safety(),
    }


def _safety():
    return {
        "offline_only": True,
        "order_submission": False,
        "order_creation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_execution": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
