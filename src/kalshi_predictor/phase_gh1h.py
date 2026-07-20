from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import httpx

from kalshi_predictor.kalshi.orderbook import LocalOrderbook, parse_orderbook
from kalshi_predictor.utils.time import utc_now

PRODUCTION_PUBLIC_REST_URL = "https://api.elections.kalshi.com/trade-api/v2"


def analyze_public_orderbook(*, ticker: str, category: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    book = LocalOrderbook(ticker)
    book.apply_rest_snapshot(payload, resume_sequence=0)
    legacy = parse_orderbook(dict(payload))
    yes_depth = book.depth(side="yes", levels=5)
    no_depth = book.depth(side="no", levels=5)
    yes_buy = book.execution_quote(outcome="yes", action="buy", size=Decimal("1"))
    spread_pass = book.spread is not None and book.spread <= Decimal("0.02")
    depth_pass = yes_depth >= 1 and no_depth >= 1
    executable_pass = yes_buy.fully_executable
    risk_gate_pass = spread_pass and depth_pass and executable_pass
    return {
        "ticker": ticker,
        "category": category,
        "best_yes_bid": _string(book.best_yes_bid),
        "best_yes_ask": _string(book.best_yes_ask),
        "spread": _string(book.spread),
        "yes_depth_5": _string(yes_depth),
        "no_depth_5": _string(no_depth),
        "imbalance": _string(book.imbalance),
        "yes_buy_1": _string(yes_buy.average_price),
        "yes_buy_1_fully_executable": executable_pass,
        "legacy_parser_consistent": (
            legacy.best_yes_bid == book.best_yes_bid
            and legacy.best_yes_ask == book.best_yes_ask
            and legacy.spread == book.spread
        ),
        "ranking_effect": {
            "liquidity_usable": risk_gate_pass,
            "spread_pass": spread_pass,
            "depth_pass": depth_pass,
        },
        "risk_effect": {
            "gate_pass": risk_gate_pass,
            "executable_price_present": yes_buy.average_price is not None,
        },
    }


def write_gh1h_report(
    *,
    output_dir: Path,
    series: list[str],
    max_markets_per_series: int,
    max_quoted_per_category: int,
    rest_base_url: str = PRODUCTION_PUBLIC_REST_URL,
) -> Path:
    rows: list[dict[str, Any]] = []
    scanned = 0
    with httpx.Client(base_url=rest_base_url, timeout=15.0) as client:
        for series_ticker in series:
            category = _category(series_ticker)
            if sum(row["category"] == category for row in rows) >= max_quoted_per_category:
                continue
            response = client.get(
                "/markets",
                params={"limit": max_markets_per_series, "status": "open", "series_ticker": series_ticker},
            )
            response.raise_for_status()
            for market in response.json().get("markets", [])[:max_markets_per_series]:
                ticker = str(market.get("ticker") or "")
                if not ticker:
                    continue
                scanned += 1
                book_response = client.get(f"/markets/{ticker}/orderbook", params={"depth": 5})
                book_response.raise_for_status()
                payload = book_response.json()
                container = payload.get("orderbook_fp", {})
                if not (container.get("yes_dollars") or container.get("no_dollars")):
                    continue
                rows.append(analyze_public_orderbook(ticker=ticker, category=category, payload=payload))
                if sum(row["category"] == category for row in rows) >= max_quoted_per_category:
                    break
    report = {
        "phase": "GH-1H",
        "generated_at": utc_now().isoformat(),
        "mode": "UNAUTHENTICATED_PRODUCTION_PUBLIC_REST_READ_ONLY",
        "rest_base_url": rest_base_url,
        "execution_enabled": False,
        "database_writes": 0,
        "orders_submitted": 0,
        "series_scanned": series,
        "markets_scanned": scanned,
        "quoted_markets": rows,
        "summary": {
            "quoted_total": len(rows),
            "weather_quoted": sum(row["category"] == "weather" for row in rows),
            "crypto_quoted": sum(row["category"] == "crypto" for row in rows),
            "legacy_parser_consistent": sum(row["legacy_parser_consistent"] for row in rows),
            "ranking_liquidity_usable": sum(row["ranking_effect"]["liquidity_usable"] for row in rows),
            "risk_gate_pass": sum(row["risk_effect"]["gate_pass"] for row in rows),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1h_production_public_liquidity_calibration.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _category(series_ticker: str) -> str:
    return "crypto" if series_ticker.upper().startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE")) else "weather"


def _string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
