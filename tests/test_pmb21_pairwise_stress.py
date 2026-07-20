import json

from kalshi_predictor.benchmarking.pairwise_stress import (
    FACTOR_PAIRS,
    build_pairwise_stress_interaction_matrix,
    write_pairwise_stress_interaction_matrix,
)


def test_pmb21_is_deterministic_local_and_frozen(tmp_path):
    first = json.loads(write_pairwise_stress_interaction_matrix(tmp_path / "a").read_text())
    second = json.loads(write_pairwise_stress_interaction_matrix(tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["policy_retrained"] is False


def test_pmb21_covers_requested_pairs_and_depth_interactions():
    report = build_pairwise_stress_interaction_matrix()
    observed = {(row["factor_a"], row["factor_b"]) for row in report["pairs"]}
    assert observed == set(FACTOR_PAIRS)
    assert report["summary"]["pair_count"] == 6
    assert report["summary"]["cell_count"] == 54
    assert report["summary"]["all_attribution_complete"] is True


def test_pmb21_classifies_every_cell_and_reconciles_counts():
    report = build_pairwise_stress_interaction_matrix()
    total = sum(
        report["summary"][key]
        for key in ("compounding_cells", "additive_cells", "offsetting_cells")
    )
    assert total == report["summary"]["cell_count"]
    assert all(
        cell["classification"] in {"COMPOUNDING", "ADDITIVE", "OFFSETTING"}
        for pair in report["pairs"] for cell in pair["cells"]
    )
