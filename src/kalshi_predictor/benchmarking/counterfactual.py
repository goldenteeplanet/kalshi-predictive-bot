from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.portfolio import (
    PortfolioLimits,
    _allocation_blocker,
    _exposure_summary,
    _limits_respected,
    _marked_equity,
    _portfolio_episode,
)
from kalshi_predictor.benchmarking.provenance_portfolio import SYNTHETIC_ATTRIBUTION
from kalshi_predictor.benchmarking.replay import replay_episode
from kalshi_predictor.kalshi.orderbook import LocalOrderbook

BASELINE_FORECASTS = {
    "SYN-BTC": Decimal("0.62"),
    "SYN-NYC-WEATHER": Decimal("0.56"),
    "SYN-SPORTS": Decimal("0.57"),
}
CANDIDATE_FORECASTS = {
    "SYN-BTC": Decimal("0.58"),
    "SYN-NYC-WEATHER": Decimal("0.66"),
    "SYN-SPORTS": Decimal("0.52"),
}
BASELINE_VERSIONS = {"crypto_v2": "2.0.0", "weather_v2": "2.0.0", "sports_v1": "1.0.0"}
CANDIDATE_VERSIONS = {"crypto_v2": "2.1.0", "weather_v2": "2.1.0", "sports_v1": "1.1.0"}


def build_counterfactual_model_comparison(
    *,
    baseline_forecasts: dict[str, Decimal] | None = None,
    candidate_forecasts: dict[str, Decimal] | None = None,
    baseline_versions: dict[str, str] | None = None,
    candidate_versions: dict[str, str] | None = None,
    limits: PortfolioLimits | None = None,
) -> dict[str, Any]:
    policy = limits or PortfolioLimits()
    baseline_input = baseline_forecasts or BASELINE_FORECASTS
    candidate_input = candidate_forecasts or CANDIDATE_FORECASTS
    baseline_model_versions = baseline_versions or BASELINE_VERSIONS
    candidate_model_versions = candidate_versions or CANDIDATE_VERSIONS
    _validate_inputs(baseline_input, baseline_model_versions)
    _validate_inputs(candidate_input, candidate_model_versions)
    baseline = _run_variant("baseline", baseline_input, baseline_model_versions, policy)
    candidate = _run_variant("candidate", candidate_input, candidate_model_versions, policy)
    changes = _decision_changes(baseline, candidate)
    canonical = json.dumps(
        {"baseline": baseline, "candidate": candidate, "changes": changes},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return {
        "phase": "PMB-11",
        "mode": "LOCAL_SYNTHETIC_ATTRIBUTION_AWARE_COUNTERFACTUAL",
        "database_access": False,
        "database_writes": 0,
        "external_replay_data_used": False,
        "execution_enabled": False,
        "limits": {key: str(value) for key, value in asdict(policy).items()},
        "baseline": baseline,
        "candidate": candidate,
        "changed_decisions": changes,
        "comparison": {
            "trade_count_delta": (
                candidate["metrics"]["trade_count"] - baseline["metrics"]["trade_count"]
            ),
            "allocated_capital_delta": str(
                Decimal(candidate["metrics"]["allocated_capital"])
                - Decimal(baseline["metrics"]["allocated_capital"])
            ),
            "pnl_delta": str(
                Decimal(candidate["metrics"]["final_pnl"])
                - Decimal(baseline["metrics"]["final_pnl"])
            ),
            "max_drawdown_delta": str(
                Decimal(candidate["metrics"]["max_drawdown"])
                - Decimal(baseline["metrics"]["max_drawdown"])
            ),
            "changed_decision_count": len(changes),
            "all_changes_attributed": all(row["attribution_complete"] for row in changes),
        },
        "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
    }


def write_counterfactual_model_comparison(output_dir: Path) -> Path:
    report = build_counterfactual_model_comparison()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb11_attribution_aware_counterfactual.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _run_variant(
    name: str,
    forecasts: dict[str, Decimal],
    versions: dict[str, str],
    limits: PortfolioLimits,
) -> dict[str, Any]:
    episode, categories = _portfolio_episode()
    frames = replay_episode(episode)
    correlation = {
        "SYN-BTC": "macro", "SYN-NYC-WEATHER": "event_risk", "SYN-SPORTS": "event_risk"
    }
    cash = limits.initial_cash
    positions: dict[str, Decimal] = {}
    costs: dict[str, Decimal] = {}
    books: dict[str, LocalOrderbook] = {}
    decisions = []
    curve = []
    for frame in frames:
        book = LocalOrderbook(frame.ticker)
        book.apply_rest_snapshot(frame.orderbook, resume_sequence=frame.sequence or 0)
        books[frame.ticker] = book
        if frame.ticker not in positions:
            ask = book.best_yes_ask
            edge = forecasts[frame.ticker] - ask if ask is not None else None
            requested = min(Decimal("10"), limits.max_ticker_exposure)
            blocker = _allocation_blocker(
                ticker=frame.ticker, requested=requested, categories=categories,
                correlation=correlation, costs=costs, limits=limits, cash=cash,
            )
            if edge is None or edge <= Decimal("0.02"):
                blocker = blocker or "EDGE_NOT_POSITIVE"
            allocated = Decimal("0")
            status = "REJECTED"
            if blocker is None and ask is not None:
                size = (requested / ask).quantize(Decimal("0.0001"))
                quote = book.execution_quote(outcome="yes", action="buy", size=size)
                if quote.filled_size > 0 and quote.average_price is not None:
                    allocated = quote.total_value
                    cash -= allocated
                    positions[frame.ticker] = quote.filled_size
                    costs[frame.ticker] = allocated
                    status = "ALLOCATED"
                else:
                    blocker = "INSUFFICIENT_LIQUIDITY"
            model = SYNTHETIC_ATTRIBUTION[frame.ticker]["model_name"]
            decisions.append({
                "timestamp": frame.timestamp.isoformat(), "ticker": frame.ticker,
                "category": categories[frame.ticker], "status": status, "blocker": blocker,
                "forecast_probability": str(forecasts[frame.ticker]),
                "model_name": model, "model_version": versions[model],
                "best_yes_ask": str(ask) if ask is not None else None,
                "edge": str(edge) if edge is not None else None,
                "requested_capital": str(requested), "allocated_capital": str(allocated),
                "feature_ref": SYNTHETIC_ATTRIBUTION[frame.ticker]["feature_ref"],
                "observation_ref": SYNTHETIC_ATTRIBUTION[frame.ticker]["observation_ref"],
                "orderbook_ref": {
                    **SYNTHETIC_ATTRIBUTION[frame.ticker]["orderbook_ref"],
                    "captured_at": frame.timestamp.isoformat(),
                },
            })
        curve.append({
            "timestamp": frame.timestamp.isoformat(),
            "equity": str(_marked_equity(cash, positions, books)),
        })
    final_cash = cash + sum(
        size for ticker, size in positions.items() if episode.settlements[ticker] == "yes"
    )
    exposure = _exposure_summary(costs, categories, correlation)
    return {
        "name": name,
        "forecasts": {ticker: str(value) for ticker, value in sorted(forecasts.items())},
        "model_versions": dict(sorted(versions.items())),
        "decisions": decisions,
        "positions": {ticker: str(value) for ticker, value in sorted(positions.items())},
        "equity_curve": curve,
        "gate_outcomes": _gate_counts(decisions),
        "metrics": {
            "trade_count": sum(row["status"] == "ALLOCATED" for row in decisions),
            "allocated_capital": str(sum(costs.values(), Decimal("0"))),
            "final_pnl": str(final_cash - limits.initial_cash),
            "max_drawdown": str(_max_drawdown(curve, limits.initial_cash)),
            "all_exposure_limits_respected": _limits_respected(exposure, limits),
        },
    }


def _decision_changes(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    base = {(row["ticker"], row["timestamp"]): row for row in baseline["decisions"]}
    other = {(row["ticker"], row["timestamp"]): row for row in candidate["decisions"]}
    changes = []
    for key in sorted(set(base) | set(other)):
        left, right = base.get(key), other.get(key)
        if left is None or right is None or (
            left["status"], left["blocker"], left["allocated_capital"]
        ) != (right["status"], right["blocker"], right["allocated_capital"]):
            source = right or left
            changes.append({
                "ticker": key[0], "timestamp": key[1],
                "baseline": left, "candidate": right,
                "forecast_probability_delta": (
                    str(
                        Decimal(right["forecast_probability"])
                        - Decimal(left["forecast_probability"])
                    )
                    if left and right else None
                ),
                "model_version_changed": bool(
                    left and right and left["model_version"] != right["model_version"]
                ),
                "attribution_complete": all(source.get(field) for field in (
                    "feature_ref", "observation_ref", "orderbook_ref", "model_version"
                )),
            })
    return changes


def _gate_counts(decisions: list[dict[str, Any]]) -> dict[str, int]:
    keys = sorted({str(row["blocker"] or row["status"]) for row in decisions})
    return {
        key: sum(str(row["blocker"] or row["status"]) == key for row in decisions)
        for key in keys
    }


def _max_drawdown(curve: list[dict[str, str]], initial: Decimal) -> Decimal:
    peak = initial
    drawdown = Decimal("0")
    for row in curve:
        equity = Decimal(row["equity"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _validate_inputs(forecasts: dict[str, Decimal], versions: dict[str, str]) -> None:
    expected = set(SYNTHETIC_ATTRIBUTION)
    if set(forecasts) != expected:
        raise ValueError("forecasts must cover the exact synthetic ticker set")
    required_models = {row["model_name"] for row in SYNTHETIC_ATTRIBUTION.values()}
    if set(versions) != required_models or not all(versions.values()):
        raise ValueError("model versions must cover the exact synthetic model set")
