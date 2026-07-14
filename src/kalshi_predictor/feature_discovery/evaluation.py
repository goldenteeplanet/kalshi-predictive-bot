from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from statistics import mean

from kalshi_predictor.feature_discovery.contracts import (
    ACTION_COLLECT_MORE_DATA,
    ACTION_NO_ACTION,
    ACTION_RUN_SHADOW_EXPERIMENT,
    STATUS_REJECTED,
    STATUS_VALIDATED,
    STATUS_WATCHLIST,
    CandidateDefinition,
    CandidateEvaluation,
    DiscoveryDatasetRow,
    FeatureDiscoveryConfig,
)
from kalshi_predictor.feature_discovery.splitter import (
    TemporalFold,
    build_purged_walk_forward_folds,
)
from kalshi_predictor.utils.decimals import decimal_to_str


def evaluate_candidates(
    rows: list[DiscoveryDatasetRow],
    candidates: list[CandidateDefinition],
    *,
    config: FeatureDiscoveryConfig,
) -> list[CandidateEvaluation]:
    folds = build_purged_walk_forward_folds(rows, config=config)
    raw_results = [
        _evaluate_one(rows, candidate, config=config, folds=folds)
        for candidate in candidates
    ]
    q_values = _benjamini_hochberg([_pseudo_pvalue(result) for result in raw_results])
    adjusted: list[CandidateEvaluation] = []
    for result, q_value in zip(raw_results, q_values, strict=True):
        adjusted.append(_apply_final_status(result, q_value=q_value, config=config))
    return sorted(
        adjusted,
        key=lambda item: (
            item.status != STATUS_VALIDATED,
            -item.composite_score,
            item.candidate.feature_name,
        ),
    )


def candidate_value(row: DiscoveryDatasetRow, candidate: CandidateDefinition) -> Decimal | None:
    expression = candidate.expression
    operator = expression["operator"]
    sources = expression["sources"]
    if operator in {"raw", "rank"}:
        return row.feature_values.get(sources[0])
    if operator == "safe_divide":
        numerator = row.feature_values.get(sources[0])
        denominator = row.feature_values.get(sources[1])
        if numerator is None or denominator is None:
            return None
        if denominator == 0:
            return Decimal("0") if expression.get("zero_policy") == "zero" else None
        return numerator / denominator
    if operator == "interaction":
        values = [row.feature_values.get(source) for source in sources]
        if any(value is None for value in values):
            return None
        product = Decimal("1")
        for value in values:
            product *= value or Decimal("0")
        return product
    if operator == "trailing_mean":
        return row.feature_values.get(sources[0])
    return None


def _evaluate_one(
    rows: list[DiscoveryDatasetRow],
    candidate: CandidateDefinition,
    *,
    config: FeatureDiscoveryConfig,
    folds: list[TemporalFold],
) -> CandidateEvaluation:
    usable = [(row, candidate_value(row, candidate)) for row in rows]
    usable = [(row, value) for row, value in usable if value is not None]
    sample_size = len(usable)
    reason_codes: list[str] = []
    if sample_size < config.min_samples:
        reason_codes.append("minimum_sample_gate_failed")
    baseline_rate = _mean_decimal(row.outcome_value for row, _ in usable)
    candidate_rate, paired_delta = _top_bottom_delta(usable)
    economic_effect = _economic_effect(usable)
    if economic_effect is None:
        reason_codes.append("economic_value_unavailable")
    fold_results = _fold_results(candidate, folds)
    stability_score = _stability_score(fold_results)
    if folds and stability_score < Decimal("0.5"):
        reason_codes.append("unstable_across_folds")
    if not folds:
        reason_codes.append("temporal_folds_unavailable")
    if paired_delta is None or paired_delta.copy_abs() < config.min_practical_effect:
        reason_codes.append("practical_effect_gate_failed")
    composite = _composite_score(
        paired_delta=paired_delta,
        economic_effect=economic_effect,
        stability_score=stability_score,
        sample_size=sample_size,
    )
    segment_results = _segment_results(usable)
    return CandidateEvaluation(
        candidate=candidate,
        status=STATUS_REJECTED,
        reason_codes=reason_codes,
        sample_size=sample_size,
        baseline_rate=baseline_rate,
        candidate_rate=candidate_rate,
        paired_delta=paired_delta,
        economic_effect=economic_effect,
        stability_score=stability_score,
        q_value=None,
        composite_score=composite,
        fold_results=fold_results,
        segment_results=segment_results,
        relationship_notes=[],
        recommendation_action=ACTION_NO_ACTION,
    )


def _apply_final_status(
    result: CandidateEvaluation,
    *,
    q_value: Decimal,
    config: FeatureDiscoveryConfig,
) -> CandidateEvaluation:
    reasons = list(result.reason_codes)
    if q_value > config.q_value_threshold:
        reasons.append("q_value_gate_failed")

    hard_failed = {
        "minimum_sample_gate_failed",
        "practical_effect_gate_failed",
        "q_value_gate_failed",
    } & set(reasons)
    if hard_failed:
        status = STATUS_REJECTED
        action = (
            ACTION_COLLECT_MORE_DATA
            if "minimum_sample_gate_failed" in hard_failed
            else ACTION_NO_ACTION
        )
    elif "economic_value_unavailable" in reasons or "temporal_folds_unavailable" in reasons:
        status = STATUS_WATCHLIST
        action = ACTION_COLLECT_MORE_DATA
    elif "unstable_across_folds" in reasons:
        status = STATUS_WATCHLIST
        action = ACTION_COLLECT_MORE_DATA
    else:
        status = STATUS_VALIDATED
        action = ACTION_RUN_SHADOW_EXPERIMENT
        reasons.append("human_review_required")

    return CandidateEvaluation(
        candidate=result.candidate,
        status=status,
        reason_codes=sorted(set(reasons)),
        sample_size=result.sample_size,
        baseline_rate=result.baseline_rate,
        candidate_rate=result.candidate_rate,
        paired_delta=result.paired_delta,
        economic_effect=result.economic_effect,
        stability_score=result.stability_score,
        q_value=q_value,
        composite_score=result.composite_score,
        fold_results=result.fold_results,
        segment_results=result.segment_results,
        relationship_notes=result.relationship_notes,
        recommendation_action=action,
    )


def _top_bottom_delta(
    rows: list[tuple[DiscoveryDatasetRow, Decimal]],
) -> tuple[Decimal | None, Decimal | None]:
    if len(rows) < 2:
        return None, None
    ordered = sorted(rows, key=lambda item: (item[1], item[0].row_id))
    midpoint = len(ordered) // 2
    low = ordered[:midpoint]
    high = ordered[midpoint:]
    high_rate = _mean_decimal(row.outcome_value for row, _ in high)
    low_rate = _mean_decimal(row.outcome_value for row, _ in low)
    if high_rate is None or low_rate is None:
        return high_rate, None
    return high_rate, high_rate - low_rate


def _economic_effect(rows: list[tuple[DiscoveryDatasetRow, Decimal]]) -> Decimal | None:
    pnl_rows = [(row, value) for row, value in rows if row.net_pnl is not None]
    if len(pnl_rows) < 2:
        return None
    ordered = sorted(pnl_rows, key=lambda item: (item[1], item[0].row_id))
    midpoint = len(ordered) // 2
    low = ordered[:midpoint]
    high = ordered[midpoint:]
    high_mean = _mean_decimal(row.net_pnl for row, _ in high if row.net_pnl is not None)
    low_mean = _mean_decimal(row.net_pnl for row, _ in low if row.net_pnl is not None)
    if high_mean is None or low_mean is None:
        return None
    return high_mean - low_mean


def _fold_results(
    candidate: CandidateDefinition,
    folds: list[TemporalFold],
) -> list[dict[str, str | int | None]]:
    results = []
    for fold in folds:
        validation = [
            (row, candidate_value(row, candidate))
            for row in fold.validation_rows
            if candidate_value(row, candidate) is not None
        ]
        _, delta = _top_bottom_delta(validation)
        results.append(
            {
                "fold_id": fold.fold_id,
                "train_sample_size": len(fold.train_rows),
                "validation_sample_size": len(validation),
                "train_start": fold.train_start.isoformat() if fold.train_start else None,
                "train_end": fold.train_end.isoformat() if fold.train_end else None,
                "validation_start": (
                    fold.validation_start.isoformat() if fold.validation_start else None
                ),
                "validation_end": fold.validation_end.isoformat() if fold.validation_end else None,
                "paired_delta": decimal_to_str(delta) if delta is not None else None,
            }
        )
    return results


def _stability_score(fold_results: list[dict[str, str | int | None]]) -> Decimal:
    deltas = [Decimal(str(item["paired_delta"])) for item in fold_results if item["paired_delta"]]
    if not deltas:
        return Decimal("0")
    positive = sum(1 for delta in deltas if delta > 0)
    negative = sum(1 for delta in deltas if delta < 0)
    return Decimal(max(positive, negative)) / Decimal(len(deltas))


def _segment_results(
    rows: list[tuple[DiscoveryDatasetRow, Decimal]],
) -> list[dict[str, str | int | None]]:
    output = []
    by_mode: dict[str, list[tuple[DiscoveryDatasetRow, Decimal]]] = {}
    for row, value in rows:
        by_mode.setdefault(row.execution_mode, []).append((row, value))
    for execution_mode, segment_rows in sorted(by_mode.items()):
        rate = _mean_decimal(row.outcome_value for row, _ in segment_rows)
        output.append(
            {
                "segment_key": "execution_mode",
                "segment_value": execution_mode,
                "status": "INSUFFICIENT" if len(segment_rows) < 3 else "EVALUATED",
                "sample_size": len(segment_rows),
                "outcome_rate": decimal_to_str(rate) if rate is not None else None,
            }
        )
    return output


def _composite_score(
    *,
    paired_delta: Decimal | None,
    economic_effect: Decimal | None,
    stability_score: Decimal,
    sample_size: int,
) -> Decimal:
    predictive = (paired_delta or Decimal("0")).copy_abs()
    economic = min((economic_effect or Decimal("0")).copy_abs(), Decimal("1"))
    sample_quality = min(Decimal(sample_size) / Decimal("100"), Decimal("1"))
    return (predictive * Decimal("40")) + (economic * Decimal("30")) + (
        stability_score * Decimal("20")
    ) + (sample_quality * Decimal("10"))


def _pseudo_pvalue(result: CandidateEvaluation) -> Decimal:
    if result.paired_delta is None or result.sample_size <= 0:
        return Decimal("1")
    strength = result.paired_delta.copy_abs() * Decimal(result.sample_size)
    return Decimal("1") / (Decimal("1") + strength)


def _benjamini_hochberg(p_values: list[Decimal]) -> list[Decimal]:
    if not p_values:
        return []
    indexed = sorted(enumerate(p_values), key=lambda item: item[1])
    total = Decimal(len(indexed))
    adjusted = [Decimal("1")] * len(indexed)
    running_min = Decimal("1")
    for rank_from_end, (original_index, p_value) in enumerate(reversed(indexed), start=1):
        rank = total - Decimal(rank_from_end) + Decimal("1")
        q_value = min(running_min, (p_value * total) / rank)
        running_min = q_value
        adjusted[original_index] = min(q_value, Decimal("1"))
    return adjusted


def _mean_decimal(values: Iterable[Decimal | None]) -> Decimal | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return Decimal(str(mean(clean)))
