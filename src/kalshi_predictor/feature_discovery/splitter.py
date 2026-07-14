from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from kalshi_predictor.feature_discovery.contracts import (
    DiscoveryDatasetRow,
    FeatureDiscoveryConfig,
)


@dataclass(frozen=True)
class TemporalFold:
    fold_id: str
    train_rows: tuple[DiscoveryDatasetRow, ...]
    validation_rows: tuple[DiscoveryDatasetRow, ...]

    @property
    def train_start(self):
        return min((row.decision_timestamp for row in self.train_rows), default=None)

    @property
    def train_end(self):
        return max((row.decision_timestamp for row in self.train_rows), default=None)

    @property
    def validation_start(self):
        return min((row.decision_timestamp for row in self.validation_rows), default=None)

    @property
    def validation_end(self):
        return max((row.decision_timestamp for row in self.validation_rows), default=None)


def build_purged_walk_forward_folds(
    rows: list[DiscoveryDatasetRow],
    *,
    config: FeatureDiscoveryConfig,
    max_folds: int = 3,
) -> list[TemporalFold]:
    ordered = sorted(rows, key=lambda row: (row.decision_timestamp, row.row_id))
    if len(ordered) < 2:
        return []
    fold_count = min(max_folds, max(1, len(ordered) - 1))
    validation_size = max(1, len(ordered) // (fold_count + 1))
    folds: list[TemporalFold] = []
    purge = timedelta(seconds=config.purge_seconds)
    embargo = timedelta(seconds=config.embargo_seconds)

    for index in range(fold_count):
        validation_start_index = len(ordered) - ((fold_count - index) * validation_size)
        validation_end_index = min(len(ordered), validation_start_index + validation_size)
        if validation_start_index <= 0:
            continue
        validation_rows = tuple(ordered[validation_start_index:validation_end_index])
        if not validation_rows:
            continue
        validation_start = min(row.decision_timestamp for row in validation_rows)
        validation_end = max(row.label_interval_end for row in validation_rows)
        train_candidates = ordered[:validation_start_index]
        train_rows = tuple(
            row
            for row in train_candidates
            if row.decision_timestamp < validation_start
            and not _interval_overlaps(
                row.label_interval_start,
                row.label_interval_end,
                validation_start - purge,
                validation_end + embargo,
            )
        )
        if not train_rows:
            continue
        folds.append(
            TemporalFold(
                fold_id=f"fold_{len(folds) + 1}",
                train_rows=train_rows,
                validation_rows=validation_rows,
            )
        )
    return folds


def _interval_overlaps(left_start, left_end, right_start, right_end) -> bool:
    return left_start <= right_end and right_start <= left_end
