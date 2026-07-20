"""Deterministic synthetic multi-market portfolio replay for PMB-9."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.replay import load_synthetic_episode, replay_episode
from kalshi_predictor.benchmarking.scenarios import synthetic_scenarios
from kalshi_predictor.kalshi.orderbook import LocalOrderbook


@dataclass(frozen=True)
class PortfolioLimits:
    initial_cash: Decimal = Decimal("300")
    max_gross_exposure: Decimal = Decimal("90")
    max_category_exposure: Decimal = Decimal("40")
    max_correlated_exposure: Decimal = Decimal("55")
    max_ticker_exposure: Decimal = Decimal("25")


def write_portfolio_benchmark(output_dir: Path, limits: PortfolioLimits | None = None) -> Path:
    policy = limits or PortfolioLimits()
    episode, categories = _portfolio_episode()
    frames = replay_episode(episode)
    forecasts = {"SYN-BTC": Decimal("0.62"), "SYN-NYC-WEATHER": Decimal("0.56"),
                 "SYN-SPORTS": Decimal("0.57")}
    correlation = {"SYN-BTC": "macro", "SYN-NYC-WEATHER": "event_risk",
                   "SYN-SPORTS": "event_risk"}
    cash = policy.initial_cash
    positions: dict[str, Decimal] = {}
    costs: dict[str, Decimal] = {}
    books: dict[str, LocalOrderbook] = {}
    decisions: list[dict[str, Any]] = []
    curve: list[dict[str, str]] = []

    for frame in frames:
        book = LocalOrderbook(frame.ticker)
        book.apply_rest_snapshot(frame.orderbook, resume_sequence=frame.sequence or 0)
        books[frame.ticker] = book
        if frame.ticker not in positions:
            ask = book.best_yes_ask
            edge = forecasts[frame.ticker] - ask if ask is not None else None
            requested = min(Decimal("10"), policy.max_ticker_exposure)
            blocker = _allocation_blocker(
                ticker=frame.ticker, requested=requested, categories=categories,
                correlation=correlation, costs=costs, limits=policy, cash=cash,
            )
            if edge is None or edge <= Decimal("0.02"):
                blocker = blocker or "EDGE_NOT_POSITIVE"
            if blocker is None and ask is not None:
                size = (requested / ask).quantize(Decimal("0.0001"))
                quote = book.execution_quote(outcome="yes", action="buy", size=size)
                if quote.filled_size > 0 and quote.average_price is not None:
                    spent = quote.total_value
                    cash -= spent
                    positions[frame.ticker] = quote.filled_size
                    costs[frame.ticker] = spent
                    decisions.append(_decision(frame, requested, spent, "ALLOCATED", None))
                else:
                    decisions.append(_decision(frame, requested, Decimal("0"), "REJECTED",
                                               "INSUFFICIENT_LIQUIDITY"))
            else:
                decisions.append(_decision(frame, requested, Decimal("0"), "REJECTED", blocker))
        curve.append({"timestamp": frame.timestamp.isoformat(),
                      "equity": str(_marked_equity(cash, positions, books))})

    final_cash = cash + sum(
        size for ticker, size in positions.items() if episode.settlements[ticker] == "yes"
    )
    exposure = _exposure_summary(costs, categories, correlation)
    canonical = json.dumps(decisions, sort_keys=True, separators=(",", ":")).encode()
    report = {
        "phase": "PMB-9", "mode": "LOCAL_SYNTHETIC_MULTI_MARKET_READ_ONLY",
        "database_writes": 0, "execution_enabled": False,
        "external_replay_data_used": False,
        "limits": {key: str(value) for key, value in asdict(policy).items()},
        "episode": {"episode_id": episode.episode_id, "market_count": len(categories),
                    "categories": categories},
        "allocation_decisions": decisions, "exposure": exposure,
        "positions": {ticker: str(size) for ticker, size in sorted(positions.items())},
        "initial_cash": str(policy.initial_cash), "final_cash": str(final_cash),
        "final_pnl": str(final_cash - policy.initial_cash), "equity_curve": curve,
        "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        "summary": {
            "allocated_markets": len(positions),
            "rejected_allocations": sum(row["status"] == "REJECTED" for row in decisions),
            "all_exposure_limits_respected": _limits_respected(exposure, policy),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb9_multi_market_portfolio_replay.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _portfolio_episode():
    scenarios = synthetic_scenarios()
    events, settlements, categories = [], {}, {}
    for category, payload in scenarios.items():
        events.extend(payload["events"])
        settlements.update(payload["settlements"])
        categories.update({ticker: category for ticker in payload["settlements"]})
    return load_synthetic_episode({"episode_id": "synthetic-multi-market-portfolio",
        "category": "portfolio", "events": events, "settlements": settlements}), categories


def _allocation_blocker(*, ticker: str, requested: Decimal, categories: dict[str, str],
                        correlation: dict[str, str], costs: dict[str, Decimal],
                        limits: PortfolioLimits, cash: Decimal) -> str | None:
    if requested > cash:
        return "CAPITAL_INSUFFICIENT"
    if sum(costs.values(), Decimal("0")) + requested > limits.max_gross_exposure:
        return "GROSS_EXPOSURE_LIMIT"
    category_total = sum(v for key, v in costs.items() if categories[key] == categories[ticker])
    if category_total + requested > limits.max_category_exposure:
        return "CATEGORY_EXPOSURE_LIMIT"
    group_total = sum(v for key, v in costs.items() if correlation[key] == correlation[ticker])
    if group_total + requested > limits.max_correlated_exposure:
        return "CORRELATED_EXPOSURE_LIMIT"
    if costs.get(ticker, Decimal("0")) + requested > limits.max_ticker_exposure:
        return "TICKER_EXPOSURE_LIMIT"
    return None


def _decision(frame, requested: Decimal, allocated: Decimal, status: str,
              blocker: str | None) -> dict[str, Any]:
    return {"timestamp": frame.timestamp.isoformat(), "ticker": frame.ticker,
            "requested_capital": str(requested), "allocated_capital": str(allocated),
            "status": status, "blocker": blocker}


def _marked_equity(cash: Decimal, positions: dict[str, Decimal],
                   books: dict[str, LocalOrderbook]) -> Decimal:
    value = cash
    for ticker, size in positions.items():
        midpoint = books.get(ticker).midpoint if ticker in books else None
        if midpoint is not None:
            value += size * midpoint
    return value


def _exposure_summary(costs: dict[str, Decimal], categories: dict[str, str],
                      correlation: dict[str, str]) -> dict[str, Any]:
    by_category = {category: str(sum(v for k, v in costs.items() if categories[k] == category))
                   for category in sorted(set(categories.values()))}
    by_group = {group: str(sum(v for k, v in costs.items() if correlation[k] == group))
                for group in sorted(set(correlation.values()))}
    return {"gross": str(sum(costs.values(), Decimal("0"))),
            "by_ticker": {k: str(v) for k, v in sorted(costs.items())},
            "by_category": by_category, "by_correlation_group": by_group}


def _limits_respected(exposure: dict[str, Any], limits: PortfolioLimits) -> bool:
    return (
        Decimal(exposure["gross"]) <= limits.max_gross_exposure
        and all(Decimal(v) <= limits.max_ticker_exposure for v in exposure["by_ticker"].values())
        and all(Decimal(v) <= limits.max_category_exposure for v in exposure["by_category"].values())
        and all(Decimal(v) <= limits.max_correlated_exposure
                for v in exposure["by_correlation_group"].values())
    )
