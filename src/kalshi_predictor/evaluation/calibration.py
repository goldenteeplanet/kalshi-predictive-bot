from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationBin:
    bin_start: float
    bin_end: float
    count: int
    avg_predicted_probability: float
    observed_frequency: float


def calibration_bins(
    y_true: Sequence[int],
    y_prob: Sequence[float],
    n_bins: int = 10,
) -> list[CalibrationBin]:
    if len(y_true) != len(y_prob):
        raise ValueError("y_true and y_prob must have the same length.")
    if n_bins <= 0:
        raise ValueError("n_bins must be positive.")

    bins: list[CalibrationBin] = []
    width = 1.0 / n_bins

    for index in range(n_bins):
        bin_start = index * width
        bin_end = 1.0 if index == n_bins - 1 else (index + 1) * width
        members = [
            (actual, probability)
            for actual, probability in zip(y_true, y_prob, strict=True)
            if bin_start <= probability <= bin_end
            if index == n_bins - 1 or probability < bin_end
        ]
        count = len(members)
        if count:
            avg_probability = sum(probability for _, probability in members) / count
            observed_frequency = sum(actual for actual, _ in members) / count
        else:
            avg_probability = 0.0
            observed_frequency = 0.0
        bins.append(
            CalibrationBin(
                bin_start=bin_start,
                bin_end=bin_end,
                count=count,
                avg_predicted_probability=avg_probability,
                observed_frequency=observed_frequency,
            )
        )

    return bins
