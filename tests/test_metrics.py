import math

from kalshi_predictor.evaluation.metrics import accuracy_at_threshold, brier_score, log_loss


def test_brier_score() -> None:
    assert brier_score([1, 0], [0.8, 0.2]) == 0.039999999999999994


def test_log_loss() -> None:
    result = log_loss([1, 0], [0.8, 0.2])
    assert math.isclose(result, 0.2231435513142097)


def test_accuracy_at_threshold() -> None:
    assert accuracy_at_threshold([1, 0, 1], [0.8, 0.4, 0.49]) == 2 / 3

