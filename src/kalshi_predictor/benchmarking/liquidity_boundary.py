from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.counterfactual import BASELINE_FORECASTS
from kalshi_predictor.benchmarking.gate_margin import EDGE_THRESHOLD
from kalshi_predictor.benchmarking.provenance_portfolio import SYNTHETIC_ATTRIBUTION
from kalshi_predictor.kalshi.orderbook import LocalOrderbook

SPREADS = (Decimal("0.02"), Decimal("0.08"), Decimal("0.20"))
TOP_FIVE_DEPTHS = (Decimal("0"), Decimal("5"), Decimal("25"))
YES_BIDS = {
    "SYN-BTC": Decimal("0.40"),
    "SYN-NYC-WEATHER": Decimal("0.48"),
    "SYN-SPORTS": Decimal("0.30"),
}
REQUESTED_CAPITAL = Decimal("10")


def build_liquidity_boundary_sweep(
    *,
    spreads: tuple[Decimal, ...] = SPREADS,
    top_five_depths: tuple[Decimal, ...] = TOP_FIVE_DEPTHS,
) -> dict[str, Any]:
    if not spreads or not top_five_depths:
        raise ValueError("spread and depth grids must be non-empty")
    if any(value < 0 or value >= 1 for value in spreads):
        raise ValueError("spreads must be between 0 and 1")
    if any(value < 0 for value in top_five_depths):
        raise ValueError("depth values must be non-negative")
    rows = []
    for ticker in YES_BIDS:
        for spread in spreads:
            for depth in top_five_depths:
                rows.append(_evaluate(ticker, spread, depth))
    rows.sort(key=lambda row: (
        row["ticker"], Decimal(row["spread"]), Decimal(row["top_five_depth"])
    ))
    boundaries = [_ticker_boundaries(ticker, rows, spreads, top_five_depths) for ticker in YES_BIDS]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-14",
        "mode": "LOCAL_SYNTHETIC_LIQUIDITY_SPREAD_DEPTH_BOUNDARY_SWEEP",
        "database_access": False,
        "database_writes": 0,
        "external_replay_data_used": False,
        "execution_enabled": False,
        "unchanged_thresholds": {
            "minimum_edge": str(EDGE_THRESHOLD),
            "requested_capital": str(REQUESTED_CAPITAL),
        },
        "grid": {
            "spreads": [str(value) for value in spreads],
            "top_five_depths": [str(value) for value in top_five_depths],
            "rows": rows,
        },
        "ticker_boundaries": boundaries,
        "summary": {
            "rows": len(rows),
            "allocated": sum(row["status"] == "ALLOCATED" for row in rows),
            "edge_blocked": sum(row["blocker"] == "EDGE_NOT_POSITIVE" for row in rows),
            "liquidity_blocked": sum(
                row["blocker"] == "INSUFFICIENT_LIQUIDITY" for row in rows
            ),
            "partial_fills": sum(row["fill_state"] == "PARTIAL" for row in rows),
            "full_fills": sum(row["fill_state"] == "FULL" for row in rows),
            "all_attribution_complete": all(row["attribution_complete"] for row in rows),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_liquidity_boundary_sweep(output_dir: Path) -> Path:
    report = build_liquidity_boundary_sweep()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb14_liquidity_spread_depth_boundaries.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _evaluate(
    ticker: str, spread: Decimal, top_five_depth: Decimal,
    forecast: Decimal | None = None,
) -> dict[str, Any]:
    selected_forecast = forecast if forecast is not None else BASELINE_FORECASTS[ticker]
    yes_bid = YES_BIDS[ticker]
    yes_ask = yes_bid + spread
    book = LocalOrderbook(ticker)
    book.apply_rest_snapshot(
        _book_payload(ticker, yes_bid, yes_ask, top_five_depth), resume_sequence=1
    )
    visible_ask = book.best_yes_ask
    edge = selected_forecast - visible_ask if visible_ask is not None else None
    blocker = None
    quote = None
    requested_size = REQUESTED_CAPITAL / yes_ask
    if visible_ask is None:
        blocker = "INSUFFICIENT_LIQUIDITY"
    elif edge is None or edge <= EDGE_THRESHOLD:
        blocker = "EDGE_NOT_POSITIVE"
    else:
        quote = book.execution_quote(outcome="yes", action="buy", size=requested_size)
        if quote.filled_size <= 0:
            blocker = "INSUFFICIENT_LIQUIDITY"
    status = "ALLOCATED" if blocker is None else "REJECTED"
    filled = quote.filled_size if quote else Decimal("0")
    fill_state = (
        "NONE" if filled == 0 else "FULL" if filled >= requested_size else "PARTIAL"
    )
    attribution = SYNTHETIC_ATTRIBUTION[ticker]
    return {
        "ticker": ticker,
        "forecast_probability": str(selected_forecast),
        "model_name": attribution["model_name"],
        "model_version": attribution["model_version"],
        "spread": str(spread),
        "top_five_depth": str(top_five_depth),
        "best_yes_bid": str(book.best_yes_bid) if book.best_yes_bid is not None else None,
        "best_yes_ask": str(visible_ask) if visible_ask is not None else None,
        "edge": str(edge) if edge is not None else None,
        "requested_size": str(requested_size),
        "filled_size": str(filled),
        "average_execution_price": (
            str(quote.average_price) if quote and quote.average_price is not None else None
        ),
        "executed_value": str(quote.total_value) if quote else "0",
        "fill_ratio": str(filled / requested_size) if requested_size else "0",
        "fill_state": fill_state,
        "status": status,
        "blocker": blocker,
        "feature_ref": attribution["feature_ref"],
        "observation_ref": attribution["observation_ref"],
        "orderbook_ref": {
            **attribution["orderbook_ref"],
            "scenario": f"spread={spread}|depth={top_five_depth}",
        },
        "attribution_complete": all(attribution.get(field) for field in (
            "feature_ref", "observation_ref", "orderbook_ref", "model_version"
        )),
    }


def _book_payload(
    ticker: str, yes_bid: Decimal, yes_ask: Decimal, total_depth: Decimal
) -> dict[str, Any]:
    level_depth = total_depth / Decimal("5")
    no_levels = []
    if total_depth > 0:
        for index in range(5):
            ask = yes_ask + Decimal(index) * Decimal("0.01")
            if ask < 1:
                no_levels.append([str(Decimal("1") - ask), str(level_depth)])
    return {
        "market_ticker": ticker,
        "yes_dollars": [[str(yes_bid), str(total_depth)]],
        "no_dollars": no_levels,
    }


def _ticker_boundaries(
    ticker: str,
    rows: list[dict[str, Any]],
    spreads: tuple[Decimal, ...],
    depths: tuple[Decimal, ...],
) -> dict[str, Any]:
    selected = [row for row in rows if row["ticker"] == ticker]
    minimum_depth_by_spread = {}
    for spread in spreads:
        allocated = [
            Decimal(row["top_five_depth"]) for row in selected
            if Decimal(row["spread"]) == spread and row["status"] == "ALLOCATED"
        ]
        minimum_depth_by_spread[str(spread)] = str(min(allocated)) if allocated else None
    maximum_spread_by_depth = {}
    for depth in depths:
        allocated = [
            Decimal(row["spread"]) for row in selected
            if Decimal(row["top_five_depth"]) == depth and row["status"] == "ALLOCATED"
        ]
        maximum_spread_by_depth[str(depth)] = str(max(allocated)) if allocated else None
    return {
        "ticker": ticker,
        "minimum_top_five_depth_for_allocation_by_spread": minimum_depth_by_spread,
        "maximum_allocating_spread_by_depth": maximum_spread_by_depth,
    }
