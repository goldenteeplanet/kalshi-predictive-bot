from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1h import (
    PRODUCTION_PUBLIC_REST_URL,
    analyze_public_orderbook,
)
from kalshi_predictor.utils.time import utc_now

TICK_SIZE = Decimal("0.01")


def calibrate_two_sided_row(row: dict[str, Any], *, settings: Settings) -> dict[str, Any]:
    spread = Decimal(str(row["spread"]))
    yes_depth = Decimal(str(row["yes_depth_5"]))
    no_depth = Decimal(str(row["no_depth_5"]))
    spread_ticks = spread / TICK_SIZE
    depth_pass = yes_depth >= Decimal("1") and no_depth >= Decimal("1")
    ranking_pass = spread <= settings.opportunity_max_spread and depth_pass
    risk_executable_pass = (
        spread_ticks <= settings.advanced_risk_spread_executable_max_ticks
        and depth_pass
        and bool(row["yes_buy_1_fully_executable"])
    )
    row["calibration"] = {
        "spread_ticks": str(spread_ticks),
        "ranking_spread_threshold": str(settings.opportunity_max_spread),
        "risk_preferred_max_ticks": str(settings.advanced_risk_spread_preferred_max_ticks),
        "risk_executable_max_ticks": str(settings.advanced_risk_spread_executable_max_ticks),
        "depth_threshold": "1",
        "ranking_advance": ranking_pass,
        "risk_preferred_advance": (
            spread_ticks <= settings.advanced_risk_spread_preferred_max_ticks and depth_pass
        ),
        "risk_executable_advance": risk_executable_pass,
    }
    return row


def write_gh1i_report(
    *,
    settings: Settings,
    output_dir: Path,
    series: list[str],
    max_markets_per_series: int,
    max_two_sided_per_category: int,
    rest_base_url: str = PRODUCTION_PUBLIC_REST_URL,
) -> Path:
    rows: list[dict[str, Any]] = []
    markets_scanned = quoted_scanned = 0
    with httpx.Client(base_url=rest_base_url, timeout=15.0) as client:
        for series_ticker in series:
            category = _category(series_ticker)
            if sum(row["category"] == category for row in rows) >= max_two_sided_per_category:
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
                markets_scanned += 1
                response = client.get(f"/markets/{ticker}/orderbook", params={"depth": 5})
                response.raise_for_status()
                payload = response.json()
                container = payload.get("orderbook_fp", {})
                if not (container.get("yes_dollars") or container.get("no_dollars")):
                    continue
                quoted_scanned += 1
                row = analyze_public_orderbook(ticker=ticker, category=category, payload=payload)
                if row["best_yes_bid"] is None or row["best_yes_ask"] is None:
                    continue
                rows.append(calibrate_two_sided_row(row, settings=settings))
                if sum(item["category"] == category for item in rows) >= max_two_sided_per_category:
                    break
    spreads = [Decimal(row["spread"]) for row in rows]
    yes_depths = [Decimal(row["yes_depth_5"]) for row in rows]
    no_depths = [Decimal(row["no_depth_5"]) for row in rows]
    report = {
        "phase": "GH-1I",
        "generated_at": utc_now().isoformat(),
        "mode": "UNAUTHENTICATED_PRODUCTION_PUBLIC_REST_READ_ONLY",
        "execution_enabled": False,
        "database_writes": 0,
        "thresholds_changed": False,
        "markets_scanned": markets_scanned,
        "quoted_markets_scanned": quoted_scanned,
        "two_sided_books": rows,
        "thresholds": {
            "opportunity_max_spread": str(settings.opportunity_max_spread),
            "advanced_risk_spread_preferred_max_ticks": str(settings.advanced_risk_spread_preferred_max_ticks),
            "advanced_risk_spread_executable_max_ticks": str(settings.advanced_risk_spread_executable_max_ticks),
            "depth_contracts": "1",
            "tick_size": str(TICK_SIZE),
        },
        "distribution": {
            "spread": _distribution(spreads),
            "yes_depth_5": _distribution(yes_depths),
            "no_depth_5": _distribution(no_depths),
        },
        "summary": {
            "two_sided_total": len(rows),
            "weather_two_sided": sum(row["category"] == "weather" for row in rows),
            "crypto_two_sided": sum(row["category"] == "crypto" for row in rows),
            "ranking_advance": sum(row["calibration"]["ranking_advance"] for row in rows),
            "risk_preferred_advance": sum(row["calibration"]["risk_preferred_advance"] for row in rows),
            "risk_executable_advance": sum(row["calibration"]["risk_executable_advance"] for row in rows),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1i_two_sided_liquidity_calibration.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _distribution(values: list[Decimal]) -> dict[str, str | int | None]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None}
    return {
        "count": len(ordered), "min": str(ordered[0]), "p25": str(_percentile(ordered, 0.25)),
        "median": str(_percentile(ordered, 0.50)), "p75": str(_percentile(ordered, 0.75)),
        "max": str(ordered[-1]),
    }


def _percentile(values: list[Decimal], fraction: float) -> Decimal:
    return values[round((len(values) - 1) * fraction)]


def _category(series_ticker: str) -> str:
    return "crypto" if series_ticker.upper().startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE")) else "weather"
