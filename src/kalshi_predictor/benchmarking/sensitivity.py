from __future__ import annotations

import hashlib
import itertools
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.counterfactual import BASELINE_FORECASTS, _run_variant
from kalshi_predictor.benchmarking.portfolio import PortfolioLimits

PERTURBATIONS = (Decimal("-0.04"), Decimal("0.00"), Decimal("0.04"))
MODEL_BY_TICKER = {
    "SYN-BTC": "crypto_v2",
    "SYN-NYC-WEATHER": "weather_v2",
    "SYN-SPORTS": "sports_v1",
}
BASE_VERSIONS = {"crypto_v2": "2.0.0", "weather_v2": "2.0.0", "sports_v1": "1.0.0"}


def build_sensitivity_grid(
    *, perturbations: tuple[Decimal, ...] = PERTURBATIONS,
    limits: PortfolioLimits | None = None,
) -> dict[str, Any]:
    if not perturbations or any(abs(value) > Decimal("0.10") for value in perturbations):
        raise ValueError("perturbations must be non-empty and bounded to +/-0.10")
    policy = limits or PortfolioLimits()
    tickers = tuple(MODEL_BY_TICKER)
    variants = []
    for values in itertools.product(perturbations, repeat=len(tickers)):
        deltas = dict(zip(tickers, values, strict=True))
        forecasts = {
            ticker: BASELINE_FORECASTS[ticker] + deltas[ticker] for ticker in tickers
        }
        versions = {
            MODEL_BY_TICKER[ticker]: _revision(MODEL_BY_TICKER[ticker], deltas[ticker])
            for ticker in tickers
        }
        variant_id = "|".join(f"{ticker}:{deltas[ticker]:+}" for ticker in tickers)
        run = _run_variant(variant_id, forecasts, versions, policy)
        allocated_categories = {
            row["category"] for row in run["decisions"] if row["status"] == "ALLOCATED"
        }
        variants.append({
            "variant_id": variant_id,
            "forecast_deltas": {ticker: str(deltas[ticker]) for ticker in tickers},
            "model_versions": versions,
            "trade_count": run["metrics"]["trade_count"],
            "allocated_capital": run["metrics"]["allocated_capital"],
            "final_pnl": run["metrics"]["final_pnl"],
            "max_drawdown": run["metrics"]["max_drawdown"],
            "category_coverage": len(allocated_categories),
            "gate_outcomes": run["gate_outcomes"],
            "ticker_outcomes": _ticker_outcomes(run["decisions"], tickers),
            "attribution_complete": all(
                all(row.get(field) for field in (
                    "feature_ref", "observation_ref", "orderbook_ref", "model_version"
                )) for row in run["decisions"]
            ),
        })
    variants.sort(key=lambda row: row["variant_id"])
    stability = _stability(variants, tickers)
    frontier_ids = _frontier(variants)
    canonical = json.dumps(variants, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-12",
        "mode": "LOCAL_SYNTHETIC_MULTI_MODEL_SENSITIVITY_FRONTIER",
        "database_access": False,
        "database_writes": 0,
        "external_replay_data_used": False,
        "execution_enabled": False,
        "grid": {
            "perturbations": [str(value) for value in perturbations],
            "variant_count": len(variants),
            "variants": variants,
        },
        "decision_stability": stability,
        "robustness_frontier": [
            row for row in variants if row["variant_id"] in frontier_ids
        ],
        "summary": {
            "stable_tickers": sum(row["classification"] == "STABLE" for row in stability),
            "fragile_tickers": sum(row["classification"] == "FRAGILE" for row in stability),
            "frontier_variants": len(frontier_ids),
            "all_attribution_complete": all(row["attribution_complete"] for row in variants),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_sensitivity_grid(output_dir: Path) -> Path:
    report = build_sensitivity_grid()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb12_multi_model_sensitivity_frontier.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _revision(model: str, delta: Decimal) -> str:
    base_major = 1 if model == "sports_v1" else 2
    minor = 0 if delta < 0 else 1 if delta > 0 else 0
    label = f"{base_major}.{minor}.0"
    return f"{label}-low" if delta < 0 else label


def _ticker_outcomes(decisions: list[dict[str, Any]], tickers: tuple[str, ...]) -> dict[str, str]:
    result = {}
    for ticker in tickers:
        rows = [row for row in decisions if row["ticker"] == ticker]
        if any(row["status"] == "ALLOCATED" for row in rows):
            result[ticker] = "ALLOCATED"
        else:
            blockers = sorted({str(row["blocker"] or "REJECTED") for row in rows})
            result[ticker] = "REJECTED:" + ",".join(blockers)
    return result


def _stability(variants: list[dict[str, Any]], tickers: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = []
    for ticker in tickers:
        outcomes = sorted({row["ticker_outcomes"][ticker] for row in variants})
        rows.append({
            "ticker": ticker,
            "classification": "STABLE" if len(outcomes) == 1 else "FRAGILE",
            "outcomes": outcomes,
            "allocated_variants": sum(
                row["ticker_outcomes"][ticker] == "ALLOCATED" for row in variants
            ),
            "total_variants": len(variants),
        })
    return rows


def _frontier(variants: list[dict[str, Any]]) -> set[str]:
    representatives: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in variants:
        point = (row["final_pnl"], row["max_drawdown"], int(row["category_coverage"]))
        current = representatives.get(point)
        if current is None or row["variant_id"] < current["variant_id"]:
            representatives[point] = row
    candidates = list(representatives.values())
    frontier = set()
    for candidate in candidates:
        pnl = Decimal(candidate["final_pnl"])
        drawdown = Decimal(candidate["max_drawdown"])
        coverage = int(candidate["category_coverage"])
        dominated = False
        for other in candidates:
            if other is candidate:
                continue
            other_pnl = Decimal(other["final_pnl"])
            other_drawdown = Decimal(other["max_drawdown"])
            other_coverage = int(other["category_coverage"])
            weakly_better = (
                other_pnl >= pnl and other_drawdown <= drawdown and other_coverage >= coverage
            )
            strictly_better = (
                other_pnl > pnl or other_drawdown < drawdown or other_coverage > coverage
            )
            if weakly_better and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.add(candidate["variant_id"])
    return frontier
