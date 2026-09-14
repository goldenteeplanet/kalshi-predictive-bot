import math

import pytest

from kalshi_predictor.crypto.multiasset_challengers import forecast_challengers


def inputs():
    return dict(
        timestamps_ms=tuple(range(1000, 3601000, 1000)),
        prices=tuple(100 * math.exp(0.0001 * math.sin(i * 0.71)) for i in range(3600)),
        decision_ms=3601000,
        target_ms=3720000,
        lower=99.9,
        upper=100.1,
        decimal_places=4,
        include_end=False,
        seed=314159,
    )


def test_all_available_challengers_are_frozen_deterministically():
    first = forecast_challengers(**inputs())
    assert first == forecast_challengers(**inputs())
    assert not first["execution_authority"]
    models = first["models"]
    for name in (
        "settlement_average_gaussian_v1",
        "settlement_average_student_t6_v1",
        "empirical_matched_average_v1",
        "existing_distribution_terminal_proxy_v1",
    ):
        assert 0 <= models[name]["probability"] <= 1
    assert models["empirical_matched_average_v1"]["matched_nonoverlapping_windows"] == 29
    assert models["empirical_matched_average_v1"]["independent_n"] is None


def test_endpoint_change_changes_average_variance_by_one_second():
    args = inputs()
    first = forecast_challengers(**args)
    args["include_end"] = True
    second = forecast_challengers(**args)
    variance_difference = second["average_stddev"] ** 2 - first["average_stddev"] ** 2
    assert variance_difference == pytest.approx(
        args["prices"][-1] ** 2 * first["variance_per_second"]
    )


def test_future_and_incomplete_history_rejected():
    args = inputs()
    args["decision_ms"] = args["target_ms"]
    with pytest.raises(ValueError, match="PREWINDOW"):
        forecast_challengers(**args)
    args = inputs()
    args["timestamps_ms"] = (0,) + args["timestamps_ms"][1:]
    with pytest.raises(ValueError, match="CONTIGUOUS"):
        forecast_challengers(**args)
