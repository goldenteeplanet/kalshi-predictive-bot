from kalshi_predictor.crypto.distribution_model import (
    DistributionInputs,
    inputs_from_features,
    threshold_probability,
)


def test_probability_above_falls_as_strike_moves_away() -> None:
    inputs = DistributionInputs(
        spot=100.0,
        volatility_per_minute=0.002,
        horizon_minutes=60,
    )

    near = threshold_probability(inputs, comparator="ABOVE", threshold=101)
    far = threshold_probability(inputs, comparator="ABOVE", threshold=110)

    assert near is not None and far is not None
    assert near > far


def test_probability_in_range_is_bounded() -> None:
    inputs = DistributionInputs(
        spot=100.0,
        volatility_per_minute=0.002,
        horizon_minutes=60,
    )

    probability = threshold_probability(inputs, comparator="RANGE", lower=99, upper=101)

    assert probability is not None
    assert 0 < probability < 1


def test_inputs_use_volatility_and_shrunk_drift() -> None:
    inputs = inputs_from_features(
        {"price": "100", "volatility_1h": "0.002", "return_1h": "0.06"},
        horizon_minutes=60,
    )

    assert inputs is not None
    assert inputs.spot == 100
    assert inputs.volatility_per_minute == 0.002
    assert inputs.drift_per_minute == 0.0001
