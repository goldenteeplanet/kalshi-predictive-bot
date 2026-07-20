from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import httpx

from kalshi_predictor.kalshi.orderbook import LocalOrderbook
from kalshi_predictor.utils.time import utc_now

DEMO_REST_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


def compare_websocket_to_rest(
    *, ticker: str, websocket_orderbook: Mapping[str, Any], rest_orderbook: Mapping[str, Any]
) -> dict[str, Any]:
    websocket = _book(ticker, websocket_orderbook)
    rest = _book(ticker, rest_orderbook)
    ws_metrics = _metrics(websocket)
    rest_metrics = _metrics(rest)
    return {
        "ticker": ticker,
        "category": _category(ticker),
        "websocket": ws_metrics,
        "rest": rest_metrics,
        "delta": {
            key: _difference(ws_metrics.get(key), rest_metrics.get(key))
            for key in ("spread", "yes_depth_5", "no_depth_5", "imbalance", "yes_buy_1")
        },
        "ranking_effect": {
            "websocket_liquidity_usable": ws_metrics["liquidity_usable"],
            "rest_liquidity_usable": rest_metrics["liquidity_usable"],
            "classification_changed": (
                ws_metrics["liquidity_usable"] != rest_metrics["liquidity_usable"]
            ),
        },
        "risk_effect": {
            "websocket_gate_pass": ws_metrics["risk_gate_pass"],
            "rest_gate_pass": rest_metrics["risk_gate_pass"],
            "gate_changed": ws_metrics["risk_gate_pass"] != rest_metrics["risk_gate_pass"],
        },
    }


def write_gh1d_report(
    *, artifacts: list[Path], output_dir: Path, rest_base_url: str = DEMO_REST_BASE_URL
) -> Path:
    comparisons: list[dict[str, Any]] = []
    with httpx.Client(base_url=rest_base_url, timeout=20.0) as client:
        for artifact in artifacts:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            ticker = str(payload.get("ticker") or "")
            if not ticker or payload.get("category") != "websocket_orderbook_snapshot":
                continue
            response = client.get(f"/markets/{ticker}/orderbook", params={"depth": 0})
            response.raise_for_status()
            comparison = compare_websocket_to_rest(
                ticker=ticker,
                websocket_orderbook=payload["orderbook"],
                rest_orderbook=response.json(),
            )
            comparison["websocket_artifact"] = str(artifact)
            comparisons.append(comparison)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "phase": "GH-1D",
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_COMPARISON",
        "execution_enabled": False,
        "orders_submitted": 0,
        "rest_base_url": rest_base_url,
        "comparisons": comparisons,
        "summary": {
            "tickers_compared": len(comparisons),
            "weather_tickers": sum(row["category"] == "weather" for row in comparisons),
            "crypto_tickers": sum(row["category"] == "crypto" for row in comparisons),
            "ranking_classification_changes": sum(
                row["ranking_effect"]["classification_changed"] for row in comparisons
            ),
            "risk_gate_changes": sum(row["risk_effect"]["gate_changed"] for row in comparisons),
        },
    }
    path = output_dir / "gh1d_liquidity_truth.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _book(ticker: str, payload: Mapping[str, Any]) -> LocalOrderbook:
    book = LocalOrderbook(ticker)
    book.apply_rest_snapshot(payload, resume_sequence=0)
    return book


def _metrics(book: LocalOrderbook) -> dict[str, Any]:
    yes_depth = book.depth(side="yes", levels=5)
    no_depth = book.depth(side="no", levels=5)
    quote = book.execution_quote(outcome="yes", action="buy", size=Decimal("1"))
    liquidity_usable = (
        book.spread is not None
        and book.spread <= Decimal("0.02")
        and yes_depth >= Decimal("1")
        and no_depth >= Decimal("1")
        and quote.fully_executable
    )
    return {
        "best_yes_bid": _string(book.best_yes_bid),
        "best_yes_ask": _string(book.best_yes_ask),
        "spread": _string(book.spread),
        "yes_depth_5": _string(yes_depth),
        "no_depth_5": _string(no_depth),
        "imbalance": _string(book.imbalance),
        "yes_buy_1": _string(quote.average_price),
        "yes_buy_1_fully_executable": quote.fully_executable,
        "liquidity_usable": liquidity_usable,
        "risk_gate_pass": liquidity_usable,
    }


def _difference(left: Any, right: Any) -> str | None:
    if left is None or right is None or isinstance(left, bool) or isinstance(right, bool):
        return None
    return str(Decimal(str(left)) - Decimal(str(right)))


def _category(ticker: str) -> str:
    upper = ticker.upper()
    if upper.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE")):
        return "crypto"
    if upper.startswith(("KXTEMP", "KXHIGH", "KXLOW", "KXRAIN", "KXSNOW")):
        return "weather"
    return "other"


def _string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
