from kalshi_predictor.research.probability_polytope import polytope_information


def test_narrow_simplex_polytope_passes_information_gate():
    result = polytope_information(
        [
            {"lower": 0.30, "upper": 0.35},
            {"lower": 0.30, "upper": 0.35},
            {"lower": 0.30, "upper": 0.40},
        ]
    )
    assert result["simplex_feasible"] is True
    assert result["mean_tightened_width"] <= 0.10
    assert result["simplex_volume_ratio_upper_bound"] <= 0.01
    assert result["gate_passed"] is True


def test_broad_simplex_polytope_fails_even_when_every_bound_is_feasible():
    result = polytope_information(
        [{"lower": 0.0, "upper": 1.0} for _ in range(3)]
    )
    assert result["simplex_feasible"] is True
    assert result["gate_passed"] is False
    assert result["checks"]["mean_tightened_width"] is False
    assert result["checks"]["simplex_volume_ratio_upper_bound"] is False


def test_infeasible_bounds_fail_closed():
    result = polytope_information(
        [{"lower": 0.7, "upper": 0.8}, {"lower": 0.6, "upper": 0.9}]
    )
    assert result["simplex_feasible"] is False
    assert result["gate_passed"] is False
