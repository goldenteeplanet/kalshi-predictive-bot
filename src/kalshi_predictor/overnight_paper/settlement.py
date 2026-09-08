"""Public REST settlement state normalization. No close-time or provisional payouts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from kalshi_predictor.overnight_paper.store import aware


def market_lifecycle(ticker: str, market: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """REST finalized + settlement_ts is final; 'settled' is a query filter.

    Reference: https://docs.kalshi.com/getting_started/market_lifecycle
    The returned normalized final record can be scored by the shadow store.
    """
    if market.get("ticker") != ticker:
        raise ValueError("SETTLEMENT_IDENTITY_MISMATCH")
    status = market.get("status")
    result: dict[str, Any] = {"ticker": ticker, "state": "AWAITING_SETTLEMENT", "final": None}
    if status == "finalized":
        if market.get("is_provisional") is True or market.get("result") not in {"yes", "no"}:
            raise ValueError("FINAL_BINARY_RESULT_REQUIRED")
        try:
            settled_at = aware(market["settlement_ts"])
            closed_at = aware(market["close_time"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("FINAL_TIMESTAMPS_REQUIRED") from exc
        if not closed_at <= settled_at <= now:
            raise ValueError("FINAL_TIME_INCONSISTENT")
        raw = json.dumps(market, sort_keys=True, separators=(",", ":"), allow_nan=False)
        result.update(
            state="FINAL_RESULT_AVAILABLE",
            final={
                "ticker": ticker,
                "status": "settled",
                "result": market["result"],
                "settled_at": settled_at.isoformat(),
                "source_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "source_kind": "PUBLIC_KALSHI_FINALIZED_MARKET",
            },
        )
    elif status in {"active", "open", "initialized", "inactive"}:
        try:
            closed = aware(market["close_time"]) <= now
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("MARKET_CLOSE_TIME_REQUIRED") from exc
        result["state"] = "MARKET_CLOSED" if closed else "OPEN"
    elif status == "closed":
        result["state"] = "MARKET_CLOSED"
    elif status not in {"determined", "disputed", "amended"}:
        raise ValueError("UNKNOWN_MARKET_LIFECYCLE")
    return result
