import json

from kalshi_predictor.benchmarking.exposure_stability import (
    STABILITY_SEEDS,
    build_multi_seed_exposure_stability_census,
    write_multi_seed_exposure_stability_census,
)


def test_pmb26_is_deterministic_local_and_frozen(tmp_path):
    first = json.loads(write_multi_seed_exposure_stability_census(tmp_path / "a").read_text())
    second = json.loads(write_multi_seed_exposure_stability_census(tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["policy_tuned_per_seed"] is False


def test_pmb26_uses_distinct_deterministic_orders_and_settlements():
    report = build_multi_seed_exposure_stability_census()
    assert report["summary"]["seed_count"] == len(STABILITY_SEEDS)
    assert len({tuple(row["episode_order"]) for row in report["seeds"]}) == len(STABILITY_SEEDS)
    assert len({tuple(row["settlement_sequence"]) for row in report["seeds"]}) > 1


def test_pmb26_all_seeds_preserve_roc_and_improve_drawdown():
    report = build_multi_seed_exposure_stability_census()
    assert report["summary"]["all_seeds_passed"] is True
    assert report["summary"]["drawdown_reduction_consistent"] is True
    assert report["summary"]["return_on_capital_preserved_consistently"] is True
    assert report["summary"]["identical_trade_selection_all_seeds"] is True
    assert report["summary"]["all_attribution_complete"] is True
