from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.benchmarking.agents import MomentumAgent, PassiveAgent, SeededRandomAgent
from kalshi_predictor.benchmarking.comparison import compare_forecast_rankings
from kalshi_predictor.benchmarking.harness import run_benchmark
from kalshi_predictor.benchmarking.replay import load_synthetic_episode
from kalshi_predictor.benchmarking.scenarios import synthetic_scenarios


def write_regression_benchmark(output_dir: Path) -> Path:
    results = []
    for payload in synthetic_scenarios().values():
        episode = load_synthetic_episode(payload)
        for agent in (PassiveAgent(), SeededRandomAgent(seed=7, trade_probability=0.5),
                      MomentumAgent(threshold=Decimal("0.01"))):
            results.append(run_benchmark(episode, agent).as_dict())
    report = {
        "phase": "PMB-4", "mode": "LOCAL_SYNTHETIC_READ_ONLY",
        "database_writes": 0, "execution_enabled": False,
        "source_code_copied": False, "bundled_external_data_used": False,
        "results": results,
        "comparison": _comparison(results),
        "forecast_ranking_regression": compare_forecast_rankings(
            [
                {"ticker": "SYN-BTC", "yes_probability": "0.51", "ranking_score": "70"},
                {"ticker": "SYN-NYC-WEATHER", "yes_probability": "0.48", "ranking_score": "65"},
                {"ticker": "SYN-SPORTS", "yes_probability": "0.55", "ranking_score": "60"},
            ],
            [
                {"ticker": "SYN-BTC", "yes_probability": "0.53", "ranking_score": "68"},
                {"ticker": "SYN-NYC-WEATHER", "yes_probability": "0.49", "ranking_score": "72"},
                {"ticker": "SYN-SPORTS", "yes_probability": "0.54", "ranking_score": "61"},
            ],
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb_regression_benchmark.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _comparison(results: list[dict]) -> dict:
    return {
        category: {
            row["agent_name"]: row["metrics"]
            for row in results if row["category"] == category
        }
        for category in ("crypto", "weather", "sports")
    }
