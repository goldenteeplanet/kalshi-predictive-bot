from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from kalshi_predictor.phase_gh1d import DEMO_REST_BASE_URL
from kalshi_predictor.utils.time import utc_now


def discover_quoted_demo_tickers(
    *,
    series: list[str],
    max_markets_per_series: int = 30,
    max_quoted_per_category: int = 3,
    rest_base_url: str = DEMO_REST_BASE_URL,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
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
                book_response = client.get(f"/markets/{ticker}/orderbook", params={"depth": 5})
                book_response.raise_for_status()
                container = book_response.json().get("orderbook_fp", {})
                yes_levels = container.get("yes_dollars") or []
                no_levels = container.get("no_dollars") or []
                if yes_levels or no_levels:
                    rows.append(
                        {
                            "ticker": ticker,
                            "series_ticker": series_ticker,
                            "category": category,
                            "yes_levels": len(yes_levels),
                            "no_levels": len(no_levels),
                        }
                    )
                if sum(row["category"] == category for row in rows) >= max_quoted_per_category:
                    break
    return {
        "phase": "GH-1E",
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_DISCOVERY",
        "execution_enabled": False,
        "orders_submitted": 0,
        "max_markets_per_series": max_markets_per_series,
        "max_quoted_per_category": max_quoted_per_category,
        "series_scanned": series,
        "quoted_tickers": rows,
        "summary": {
            "quoted_total": len(rows),
            "weather_quoted": sum(row["category"] == "weather" for row in rows),
            "crypto_quoted": sum(row["category"] == "crypto" for row in rows),
        },
    }


def write_gh1e_discovery_report(*, output_dir: Path, **kwargs: Any) -> Path:
    report = discover_quoted_demo_tickers(**kwargs)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1e_quoted_ticker_discovery.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _category(series_ticker: str) -> str:
    return "crypto" if series_ticker.upper().startswith(("KXBTC", "KXETH", "KXSOL")) else "weather"
