from __future__ import annotations

from kalshi_predictor.independent_domain_experiment import (
    build_readiness,
    rank_domains,
    summarize_ledger,
)


def test_domain_ranking_rewards_canonical_evidence() -> None:
    empty = {
        "links": 0,
        "features": 0,
        "source_rows": 0,
        "forecasts": 0,
        "settled_forecasts": 0,
        "snapshots": 0,
        "odds_rows": 0,
    }
    evidence = {
        "weather": {
            **empty,
            "links": 200,
            "features": 100,
            "source_rows": 100,
            "forecasts": 100,
            "settled_forecasts": 100,
            "snapshots": 100,
        },
        "sports": empty,
        "economic": empty,
    }
    assert rank_domains(evidence)[0]["domain"] == "weather"


def test_summary_requires_market_and_post_cost_outperformance() -> None:
    rows = [
        {
            "model_brier": 0.01,
            "market_brier": 0.04,
            "one_contract_pnl_after_fee": "0.10",
            "no_lookahead": True,
        }
        for _ in range(100)
    ]
    summary = summarize_ledger(rows)
    assert summary["outperforms_market"] is True
    assert summary["positive_post_cost"] is True


def test_activation_stays_fail_closed_even_with_good_phase7_evidence() -> None:
    summary = {
        "settled_observations": 100,
        "no_lookahead_violations": 0,
        "outperforms_market": True,
        "positive_post_cost": True,
    }
    readiness = build_readiness("weather", summary, {"guarded_counts_unchanged": True})
    assert readiness["phase7_performance_pass"] is True
    assert readiness["paper_order_creation_enabled"] is False
    assert readiness["phase8_ready"] is False
