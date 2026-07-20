import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_predictor.benchmarking.agents import (
    MomentumAgent,
    PassiveAgent,
    SeededRandomAgent,
)
from kalshi_predictor.benchmarking.comparison import compare_forecast_rankings
from kalshi_predictor.benchmarking.harness import run_benchmark
from kalshi_predictor.benchmarking.replay import (
    load_synthetic_episode,
    replay_digest,
    replay_episode,
)
from kalshi_predictor.benchmarking.reports import write_regression_benchmark
from kalshi_predictor.benchmarking.scenarios import synthetic_scenarios
from kalshi_predictor.kalshi.orderbook import OrderbookSequenceGap


def _delta_episode(gap: bool = False):
    return load_synthetic_episode({
        "episode_id": "ordered", "category": "crypto", "settlements": {"SYN": "yes"},
        "events": [
            {"timestamp": "2026-01-01T00:00:02Z", "ticker": "SYN", "kind": "delta",
             "message": {"seq": 4 if gap else 2, "msg": {"market_ticker": "SYN",
                 "side": "yes", "price_dollars": "0.40", "delta_fp": "2"}}},
            {"timestamp": "2026-01-01T00:00:01Z", "ticker": "SYN", "kind": "snapshot",
             "message": {"seq": 1, "msg": {"market_ticker": "SYN",
                 "yes_dollars": [["0.40", "5"]], "no_dollars": [["0.58", "5"]]}}},
        ],
    })


def test_pmb1_replay_is_timestamp_ordered_and_repeatable() -> None:
    episode = _delta_episode()
    first = replay_episode(episode)
    second = replay_episode(episode)
    assert [frame.sequence for frame in first] == [1, 2]
    assert replay_digest(first) == replay_digest(second)
    assert first[-1].orderbook["orderbook_fp"]["yes_dollars"] == [["0.40", "7"]]


def test_pmb1_sequence_gap_is_a_hard_failure() -> None:
    with pytest.raises(OrderbookSequenceGap):
        replay_episode(_delta_episode(gap=True))


def test_pmb2_baselines_are_deterministic() -> None:
    episode = load_synthetic_episode(synthetic_scenarios()["crypto"])
    assert run_benchmark(episode, PassiveAgent()).metrics["trade_count"] == 0
    first = run_benchmark(
        episode, SeededRandomAgent(seed=19, trade_probability=1.0)
    ).as_dict()
    second = run_benchmark(
        episode, SeededRandomAgent(seed=19, trade_probability=1.0)
    ).as_dict()
    assert first == second
    momentum = run_benchmark(episode, MomentumAgent(threshold=Decimal("0.01")))
    assert momentum.metrics["trade_count"] == 2


def test_pmb3_records_executable_fills_fees_slippage_and_equity() -> None:
    episode = load_synthetic_episode(synthetic_scenarios()["crypto"])
    result = run_benchmark(episode, MomentumAgent(threshold=Decimal("0.01")))
    assert len(result.trades) == 2
    assert all(Decimal(row.filled_size) == 1 for row in result.trades)
    assert all(Decimal(row.fee) > 0 for row in result.trades)
    assert all(Decimal(row.slippage) >= 0 for row in result.trades)
    assert result.equity_curve[-1]["timestamp"] == "settlement"
    assert "max_drawdown" in result.metrics


def test_pmb4_golden_regression_report_is_stable(tmp_path: Path) -> None:
    first = write_regression_benchmark(tmp_path / "first").read_bytes()
    second = write_regression_benchmark(tmp_path / "second").read_bytes()
    assert first == second
    assert hashlib.sha256(first).hexdigest() == (
        "97aebd2423fddcf18fb642b632aca5fdb852eae00be33af96aabb06d99ce0c12"
    )


def test_pmb4_compares_forecast_and_ranking_versions() -> None:
    comparison = compare_forecast_rankings(
        [
            {"ticker": "A", "yes_probability": "0.40", "ranking_score": "80"},
            {"ticker": "B", "yes_probability": "0.60", "ranking_score": "70"},
        ],
        [
            {"ticker": "A", "yes_probability": "0.45", "ranking_score": "60"},
            {"ticker": "B", "yes_probability": "0.58", "ranking_score": "90"},
        ],
    )
    assert comparison["rank_changed"] == 2
    assert comparison["rows"][0]["probability_change"] == "0.05"
    assert comparison["rows"][0]["rank_change"] == -1
