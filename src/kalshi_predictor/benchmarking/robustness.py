from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.benchmarking.agents import MomentumAgent, SeededRandomAgent
from kalshi_predictor.benchmarking.harness import run_benchmark
from kalshi_predictor.benchmarking.replay import load_synthetic_episode
from kalshi_predictor.benchmarking.scenarios import synthetic_scenarios


def write_robustness_matrix(output_dir: Path) -> Path:
    results = []
    configurations = (
        ("baseline", Decimal("100"), Decimal("0.07"), 7),
        ("high_fee", Decimal("100"), Decimal("0.14"), 7),
        ("low_capital", Decimal("25"), Decimal("0.07"), 19),
    )
    for category, payload in sorted(synthetic_scenarios().items()):
        episode = load_synthetic_episode(payload)
        for label, cash, fee, seed in configurations:
            for agent in (
                MomentumAgent(threshold=Decimal("0.01")),
                SeededRandomAgent(seed=seed, trade_probability=0.5),
            ):
                result = run_benchmark(
                    episode, agent, initial_cash=cash, taker_fee_rate=fee
                ).as_dict()
                results.append({
                    "category": category, "configuration": label,
                    "initial_cash": str(cash), "taker_fee_rate": str(fee),
                    "agent": agent.name, "final_equity": result["final_equity"],
                    "metrics": result["metrics"], "replay_digest": result["replay_digest"],
                })
    stable_replays = all(
        len({row["replay_digest"] for row in results if row["category"] == category}) == 1
        for category in synthetic_scenarios()
    )
    payload = {
        "phase": "PMB-9", "mode": "LOCAL_SYNTHETIC_ROBUSTNESS_MATRIX",
        "database_writes": 0, "execution_enabled": False,
        "external_data_copied": False, "results": results,
        "summary": {"runs": len(results), "categories": 3,
                    "configurations": 3, "agents": 2,
                    "replay_digest_stable_across_configurations": stable_replays},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["deterministic_digest"] = hashlib.sha256(canonical.encode()).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb9_robustness_matrix.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path
