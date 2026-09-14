"""Offline paired-event evaluation; immutable receipts, no model promotion."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PredictionRecord:
    event_id: str
    target_id: str
    model: str
    symbol: str
    horizon: str
    decision_at: datetime
    model_committed_at: datetime
    input_received_at: datetime
    prediction_recorded_at: datetime
    settlement_known_at: datetime
    prediction_sha256: str
    settlement_sha256: str
    probability: float
    outcome: int
    side: str = "YES"
    executable_price: float | None = None
    fee: float | None = None
    slippage: float | None = None
    uncertainty: float | None = None


def _aware(value: datetime) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _unit(value: float) -> bool:
    return not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def _metrics(
    rows: list[PredictionRecord], gross_ids: set[str], net_ids: set[str],
) -> dict[str, Any]:
    if not rows:
        return {"status": "UNAVAILABLE", "independent_event_n": 0}
    n = len(rows)
    losses = [
        -math.log(row.probability if row.outcome else 1 - row.probability)
        if (row.probability if row.outcome else 1 - row.probability) > 0 else math.inf
        for row in rows
    ]
    bins = [[row for row in rows if min(9, int(row.probability * 10)) == i] for i in range(10)]
    ece = sum(
        abs(sum(row.probability - row.outcome for row in bucket)) / n
        for bucket in bins if bucket
    )
    gross = [row for row in rows if row.event_id in gross_ids]
    net = [row for row in rows if row.event_id in net_ids]

    def edge(row: PredictionRecord) -> float:
        assert row.executable_price is not None
        p = row.probability if row.side == "YES" else 1 - row.probability
        return p - row.executable_price

    def net_edge(row: PredictionRecord) -> float:
        assert row.fee is not None and row.slippage is not None and row.uncertainty is not None
        return edge(row) - row.fee - row.slippage - row.uncertainty

    log_loss = sum(losses) / n
    return {
        "status": "EVALUATED_NO_PROMOTION",
        "independent_event_n": n,
        "brier": sum((row.probability - row.outcome) ** 2 for row in rows) / n,
        "log_loss": log_loss if math.isfinite(log_loss) else None,
        "log_loss_infinite": not math.isfinite(log_loss),
        "ece_10_equal_width_bins": ece,
        "gross_ev_event_n": len(gross),
        "positive_gross_ev_frequency": (
            sum(edge(row) > 0 for row in gross) / len(gross) if gross else None
        ),
        "net_ev_event_n": len(net),
        "positive_net_ev_frequency": (
            sum(net_edge(row) > 0 for row in net) / len(net) if net else None
        ),
    }


def evaluate_independent_models(
    records: Sequence[PredictionRecord], *, models: tuple[str, ...], as_of: datetime,
) -> dict[str, Any]:
    """Compare one immutable decision per independent event on the common cohort.

    Records must be prospectively committed before the label was known. Receipt
    timestamps are assertions backed by supplied hashes, not independently attested
    by this arithmetic helper. Costs remain caller-certified; missing costs never
    become zero. Probability quartiles use models[0] to keep cohorts identical.
    """
    if not _aware(as_of) or not models or len(set(models)) != len(models) or not all(models):
        raise ValueError("EXPLICIT_MODELS_AND_AWARE_AS_OF_REQUIRED")
    if len(records) > 100_000:
        raise ValueError("BOUNDED_COHORT_REQUIRED")
    by_model: dict[str, dict[str, PredictionRecord]] = {model: {} for model in models}
    for row in records:
        times = (
            row.decision_at, row.model_committed_at, row.input_received_at,
            row.prediction_recorded_at, row.settlement_known_at,
        )
        if not all(_aware(value) for value in times):
            raise ValueError("AWARE_RECEIPTS_REQUIRED")
        if not (
            row.model_committed_at <= row.prediction_recorded_at
            and row.input_received_at <= row.prediction_recorded_at <= row.decision_at
            < row.settlement_known_at <= as_of
        ):
            raise ValueError("FUTURE_RECEIPT_OR_SETTLEMENT_LEAKAGE")
        if (
            row.model not in by_model or not row.event_id or not row.target_id
            or not row.symbol or not row.horizon or not _unit(row.probability)
            or type(row.outcome) is not int or row.outcome not in {0, 1}
            or row.side not in {"YES", "NO"}
            or any(value is not None and not _unit(value) for value in (
                row.executable_price, row.fee, row.slippage, row.uncertainty,
            ))
            or any(len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                   for digest in (row.prediction_sha256, row.settlement_sha256))
        ):
            raise ValueError("INVALID_IDENTITY_PROBABILITY_COST_OR_PROVENANCE")
        if row.event_id in by_model[row.model]:
            raise ValueError("DUPLICATE_MODEL_EVENT")
        by_model[row.model][row.event_id] = row
    common = set.intersection(*(set(rows) for rows in by_model.values()))
    for event in common:
        evidence = {
            (rows[event].target_id, rows[event].symbol, rows[event].horizon,
             rows[event].decision_at, rows[event].outcome, rows[event].settlement_sha256)
            for rows in by_model.values()
        }
        if len(evidence) != 1:
            raise ValueError("UNPAIRED_EVENT_TARGET_OR_OUTCOME")
    gross_ids = {
        event for event in common
        if all(rows[event].executable_price is not None for rows in by_model.values())
    }
    net_ids = {
        event for event in gross_ids
        if all(all(value is not None for value in (
            rows[event].fee, rows[event].slippage, rows[event].uncertainty,
        )) for rows in by_model.values())
    }

    def paired_metrics(events: set[str]) -> dict[str, Any]:
        return {
            model: _metrics([rows[event] for event in sorted(events)], gross_ids, net_ids)
            for model, rows in by_model.items()
        }

    segments: dict[str, dict[str, set[str]]] = {
        name: {} for name in ("symbol", "horizon", "quartile")
    }
    for event in sorted(common):
        reference = by_model[models[0]][event]
        quartile = ("0-25", "25-50", "50-75", "75-100")[min(3, int(reference.probability * 4))]
        for dimension, value in (
            ("symbol", reference.symbol), ("horizon", reference.horizon), ("quartile", quartile),
        ):
            segments[dimension].setdefault(value, set()).add(event)
    return {
        "status": "PAIRED_COHORT" if common else "UNAVAILABLE_NO_COMMON_COHORT",
        "independent_event_n": len(common), "event_ids": sorted(common),
        "excluded_unpaired_by_model": {
            model: len(rows) - len(common) for model, rows in by_model.items()
        },
        "models": paired_metrics(common), "quartile_reference_model": models[0],
        "segments": {
            dimension: {value: paired_metrics(events) for value, events in groups.items()}
            for dimension, groups in segments.items()
        },
        "promotion_authority": False,
    }
