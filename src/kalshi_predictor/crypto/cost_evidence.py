"""Original-bound cost evidence. Integrity alone is never fee/calibration authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlsplit

from kalshi_predictor.crypto.research_costs import book_slippage


class CostEvidenceStatus(StrEnum):
    CERTIFIED = "CERTIFIED"
    ESTIMATED_WITH_SUPPORT = "ESTIMATED_WITH_SUPPORT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CostEvidenceResult:
    component: str
    value: Decimal | None
    unit: str
    method: str
    version: str
    evidence_sources: tuple[tuple[str, str], ...]
    timestamp: datetime
    status: CostEvidenceStatus
    blockers: tuple[str, ...]
    paper_support: bool


@dataclass(frozen=True)
class OriginalBook:
    """Exact HTTP body and recorded receipt; hash is computed, never asserted."""

    url: str
    payload: bytes
    received_at: datetime

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("COST_AWARE_CLOCK_REQUIRED")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("COST_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _levels(original: OriginalBook, ticker: str) -> dict[str, list[tuple[Decimal, Decimal]]]:
    _aware(original.received_at)
    url = urlsplit(original.url)
    query = parse_qs(url.query, keep_blank_values=True)
    if (
        url.scheme != "https" or url.netloc != "external-api.kalshi.com"
        or url.path != f"/trade-api/v2/markets/{ticker}/orderbook" or url.fragment
        or (url.query and (
            set(query) != {"depth"} or len(query["depth"]) != 1
            or not query["depth"][0].isascii() or not query["depth"][0].isdigit()
            or not 1 <= int(query["depth"][0]) <= 1000
        ))
        or not 0 < len(original.payload) <= 1_000_000
    ):
        raise ValueError("COST_EXACT_BOOK_ORIGINAL_REQUIRED")
    body = json.loads(original.payload, object_pairs_hook=_unique)
    if not isinstance(body, dict):
        raise ValueError("COST_BOOK_OBJECT_REQUIRED")
    containers = [key for key in ("orderbook_fp", "orderbook") if key in body]
    if len(containers) != 1 or not isinstance(body[containers[0]], dict):
        raise ValueError("COST_UNAMBIGUOUS_BOOK_REQUIRED")
    book = body[containers[0]]
    dollars = "yes_dollars" in book or "no_dollars" in book
    if dollars and ("yes" in book or "no" in book):
        raise ValueError("COST_AMBIGUOUS_PRICE_UNIT")
    result = {}
    for side in ("yes", "no"):
        rows = book.get(side + "_dollars" if dollars else side)
        if not isinstance(rows, list) or not rows:
            raise ValueError("COST_TWO_SIDED_BOOK_REQUIRED")
        levels: list[tuple[Decimal, Decimal]] = []
        seen = set()
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("COST_EXACT_LEVEL_PAIR_REQUIRED")
            # JSON floats cannot preserve original decimal precision.
            if any(type(v) not in (str, int) for v in row):
                raise ValueError("COST_ORIGINAL_DECIMAL_REQUIRED")
            price, quantity = map(Decimal, row)
            if not dollars:
                price /= 100
            if (
                not price.is_finite() or not quantity.is_finite()
                or not 0 < price < 1 or quantity <= 0 or price in seen
            ):
                raise ValueError("COST_INVALID_OR_DUPLICATE_LEVEL")
            seen.add(price)
            levels.append((price, quantity))
        result[side] = sorted(levels, reverse=True)
    if result["yes"][0][0] + result["no"][0][0] > 1:
        raise ValueError("COST_CROSSED_BOOK")
    return result


def observed_one_contract_stress(
    *, ticker: str, side: str, executable_price: Decimal,
    originals: tuple[OriginalBook, ...], decision_at: datetime,
) -> CostEvidenceResult:
    """Reconstruct depth cost and full quote range from all supplied originals.

    This is a descriptive historical stress measure, NOT calibrated expected
    slippage. Postdecision observations remain explicit and never qualify paper.
    Even a measured zero does not certify immediate-fill execution or stability.
    No caller status, freshness boolean, or hash can upgrade this result.
    """
    _aware(decision_at)
    if side not in ("YES", "NO") or not ticker:
        raise ValueError("COST_EXACT_TICKER_AND_SIDE_REQUIRED")
    if (
        not isinstance(executable_price, Decimal) or not executable_price.is_finite()
        or not 0 < executable_price < 1 or not 2 <= len(originals) <= 100
    ):
        raise ValueError("COST_PRICE_AND_BOUNDED_BOOK_SEQUENCE_REQUIRED")
    books = [_levels(item, ticker) for item in originals]
    times = [item.received_at for item in originals]
    if times[0] > decision_at or any(a >= b for a, b in zip(times, times[1:], strict=False)):
        raise ValueError("COST_BOOK_RECEIPT_ORDER_INVALID")
    opposite = "no" if side == "YES" else "yes"
    depths = [tuple((1 - p, q) for p, q in book[opposite]) for book in books]
    asks = tuple(depth[0][0] for depth in depths)
    decision_index = max(i for i, at in enumerate(times) if at <= decision_at)
    if asks[decision_index] != executable_price:
        raise ValueError("COST_FROZEN_EXECUTABLE_PRICE_MISMATCH")
    evidence = tuple((item.url, item.sha256) for item in originals)
    amounts = book_slippage(
        executable_price=executable_price, depth=depths[decision_index], recent_asks=asks,
        evidence_hashes=tuple(sha for _, sha in evidence), current_and_time_ordered=True,
    )
    blockers = ["EXPECTED_SLIPPAGE_EXECUTION_MODEL_NOT_CALIBRATED"]
    if times[-1] > decision_at:
        blockers.append("POSTDECISION_BOOK_DIAGNOSTIC_ONLY")
    if any(sum((q for _, q in depth), Decimal(0)) < 1 for depth in depths):
        blockers.append("ONE_CONTRACT_DEPTH_NOT_MAINTAINED")
    if amounts.value is None:
        blockers.append(amounts.reason)
    value = amounts.value
    if value is not None:
        previous_adverse = max(Decimal(0), max(asks) - executable_price)
        value = value - previous_adverse + max(asks) - min(asks)
    return CostEvidenceResult(
        "slippage", value, "USD_PER_ONE_DOLLAR_PAYOUT", "OBSERVED_BOOK_STRESS",
        "ORIGINAL_BOOK_DEPTH_AND_FULL_RANGE_V2", evidence, times[-1],
        CostEvidenceStatus.UNKNOWN if amounts.value is None
        else CostEvidenceStatus.ESTIMATED_WITH_SUPPORT,
        tuple(blockers), False,
    )
