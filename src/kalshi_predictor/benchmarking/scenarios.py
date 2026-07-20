from __future__ import annotations

from decimal import Decimal
from typing import Any


def synthetic_scenarios() -> dict[str, dict[str, Any]]:
    return {
        category: _scenario(category, ticker, settlement, prices)
        for category, ticker, settlement, prices in (
            ("crypto", "SYN-BTC", "yes", ("0.40", "0.45", "0.52")),
            ("weather", "SYN-NYC-WEATHER", "no", ("0.65", "0.58", "0.48")),
            ("sports", "SYN-SPORTS", "yes", ("0.30", "0.31", "0.42")),
        )
    }


def _scenario(category: str, ticker: str, settlement: str,
              prices: tuple[str, ...]) -> dict[str, Any]:
    events = []
    for index, yes_bid in enumerate(prices, start=1):
        no_bid = str(Decimal("1") - Decimal(yes_bid) - Decimal("0.02"))
        events.append({
            "timestamp": f"2026-01-01T00:00:0{index}Z", "ticker": ticker,
            "kind": "snapshot",
            "message": {"seq": index, "msg": {"market_ticker": ticker,
                "yes_dollars": [[yes_bid, "10"]], "no_dollars": [[no_bid, "10"]]}},
        })
    return {"episode_id": f"synthetic-{category}", "category": category,
            "events": events, "settlements": {ticker: settlement}}
