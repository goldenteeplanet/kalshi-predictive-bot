import math
from collections.abc import Sequence


def brier_score(y_true: Sequence[int], y_prob: Sequence[float]) -> float:
    _validate_lengths(y_true, y_prob)
    return sum(
        (probability - actual) ** 2
        for actual, probability in zip(y_true, y_prob, strict=True)
    ) / len(y_true)


def log_loss(y_true: Sequence[int], y_prob: Sequence[float], eps: float = 1e-15) -> float:
    _validate_lengths(y_true, y_prob)
    total = 0.0
    for actual, probability in zip(y_true, y_prob, strict=True):
        clipped = min(1.0 - eps, max(eps, probability))
        total += actual * math.log(clipped) + (1 - actual) * math.log(1.0 - clipped)
    return -total / len(y_true)


def accuracy_at_threshold(
    y_true: Sequence[int],
    y_prob: Sequence[float],
    threshold: float = 0.5,
) -> float:
    _validate_lengths(y_true, y_prob)
    correct = sum(
        int((probability >= threshold) == bool(actual))
        for actual, probability in zip(y_true, y_prob, strict=True)
    )
    return correct / len(y_true)


def _validate_lengths(y_true: Sequence[int], y_prob: Sequence[float]) -> None:
    if len(y_true) != len(y_prob):
        raise ValueError("y_true and y_prob must have the same length.")
    if not y_true:
        raise ValueError("At least one observation is required.")
