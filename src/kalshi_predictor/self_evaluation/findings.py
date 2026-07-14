from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from kalshi_predictor.config import Settings
from kalshi_predictor.self_evaluation.contracts import (
    HUMAN_REVIEW_REQUIRED,
    FindingRecord,
    MetricRecord,
    decimal_string,
    stable_phase_3p_id,
)
from kalshi_predictor.self_evaluation.metrics import metric_by_name
from kalshi_predictor.utils.decimals import to_decimal


@dataclass(frozen=True)
class FindingSet:
    what_worked: list[FindingRecord]
    what_failed: list[FindingRecord]
    what_changed: list[FindingRecord]
    watch_items: list[FindingRecord]
    data_quality_items: list[FindingRecord]
    recommended_follow_ups: list[dict[str, Any]]


def build_findings(
    metrics: list[MetricRecord],
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> FindingSet:
    worked: list[FindingRecord] = []
    failed: list[FindingRecord] = []
    changed: list[FindingRecord] = []
    watch: list[FindingRecord] = []
    data_quality: list[FindingRecord] = []

    worked.extend(_worked_findings(metrics, evaluation_run_id=evaluation_run_id, settings=settings))
    failed.extend(_failed_findings(metrics, evaluation_run_id=evaluation_run_id, settings=settings))
    changed.extend(_changed_findings(metrics, evaluation_run_id=evaluation_run_id))
    data_quality.extend(_data_quality_findings(metrics, evaluation_run_id=evaluation_run_id))
    watch.extend(_watch_findings(metrics, evaluation_run_id=evaluation_run_id, settings=settings))
    follow_ups = _follow_ups(failed + data_quality + watch)
    return FindingSet(
        what_worked=worked[:5],
        what_failed=failed[:5],
        what_changed=changed[:5],
        watch_items=watch[:10],
        data_quality_items=data_quality[:10],
        recommended_follow_ups=follow_ups,
    )


def _worked_findings(
    metrics: list[MetricRecord],
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[FindingRecord]:
    findings = []
    direction = metric_by_name(metrics, "forecast.direction_accuracy")
    brier = metric_by_name(metrics, "forecast.brier_score")
    if direction and _supports_comparison(direction, settings):
        current = to_decimal(direction.value)
        baseline = to_decimal(direction.baseline.value)
        if current is not None and baseline is not None:
            delta = current - baseline
            if delta >= _effect_threshold(settings):
                findings.append(
                    _finding(
                        evaluation_run_id,
                        "WHAT_WORKED",
                        "FORECAST_DIRECTION_ACCURACY_IMPROVED",
                        "INFO",
                        "Forecast direction accuracy improved",
                        (
                            "Finalized forecast direction accuracy improved versus the "
                            "stored baseline."
                        ),
                        (
                            "The evidence is observational and compares the finalized forecast "
                            "cohort with prior stored Phase 3P metrics."
                        ),
                        direction,
                        current_value=direction.value,
                        absolute_delta=decimal_string(delta),
                        effect_size=decimal_string(abs(delta)),
                        attribution_level="ASSOCIATION",
                        reason_codes=["MIN_SAMPLE_MET", "PRACTICAL_EFFECT_MET"],
                    )
                )
    if brier and _supports_comparison(brier, settings):
        current = to_decimal(brier.value)
        baseline = to_decimal(brier.baseline.value)
        if current is not None and baseline is not None:
            delta = baseline - current
            if delta >= _effect_threshold(settings):
                findings.append(
                    _finding(
                        evaluation_run_id,
                        "WHAT_WORKED",
                        "FORECAST_BRIER_SCORE_IMPROVED",
                        "INFO",
                        "Forecast Brier score improved",
                        "Finalized binary forecast Brier score improved versus baseline.",
                        (
                            "Lower Brier score is better. This finding is an association, "
                            "not a causal model-quality claim."
                        ),
                        brier,
                        current_value=brier.value,
                        absolute_delta=decimal_string(-delta),
                        effect_size=decimal_string(abs(delta)),
                        attribution_level="ASSOCIATION",
                        reason_codes=["MIN_SAMPLE_MET", "PRACTICAL_EFFECT_MET"],
                    )
                )
    return findings


def _failed_findings(
    metrics: list[MetricRecord],
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[FindingRecord]:
    findings = []
    brier = metric_by_name(metrics, "forecast.brier_score")
    if brier and _supports_comparison(brier, settings):
        current = to_decimal(brier.value)
        baseline = to_decimal(brier.baseline.value)
        if current is not None and baseline is not None:
            delta = current - baseline
            if delta >= _effect_threshold(settings):
                findings.append(
                    _finding(
                        evaluation_run_id,
                        "WHAT_FAILED",
                        "FORECAST_BRIER_SCORE_REGRESSED",
                        "WARNING",
                        "Forecast Brier score regressed",
                        "Finalized binary forecast Brier score worsened versus baseline.",
                        (
                            "This is an evidence-backed regression in the evaluated cohort. "
                            "It does not assign blame to sizing or execution layers."
                        ),
                        brier,
                        current_value=brier.value,
                        absolute_delta=decimal_string(delta),
                        effect_size=decimal_string(abs(delta)),
                        attribution_level="ASSOCIATION",
                        reason_codes=["MIN_SAMPLE_MET", "PRACTICAL_EFFECT_MET"],
                        hypothesis=(
                            "Investigate whether market category mix, model version, or data "
                            "coverage coincided with the regression."
                        ),
                    )
                )
    for mode in _execution_modes(metrics):
        pnl = metric_by_name(
            metrics,
            "trade.net_pnl.total",
            cohort_key="execution_mode",
            cohort_value=mode,
        )
        if pnl and _supports_comparison(pnl, settings):
            current = to_decimal(pnl.value)
            baseline = to_decimal(pnl.baseline.value)
            if current is not None and baseline is not None:
                delta = current - baseline
                if delta <= -_effect_threshold(settings):
                    findings.append(
                        _finding(
                            evaluation_run_id,
                            "WHAT_FAILED",
                            "TRADE_NET_PNL_REGRESSED",
                            "WARNING",
                            f"{mode} net P&L underperformed baseline",
                            (
                                f"{mode} finalized trade net P&L was below the stored "
                                "execution-mode baseline."
                            ),
                            (
                                "This separates realized trade performance from forecast "
                                "direction accuracy and keeps execution modes separate."
                            ),
                            pnl,
                            current_value=pnl.value,
                            absolute_delta=decimal_string(delta),
                            effect_size=decimal_string(abs(delta)),
                            attribution_level="ASSOCIATION",
                            reason_codes=["MATCHED_EXECUTION_MODE", "PRACTICAL_EFFECT_MET"],
                            hypothesis=(
                                "Review execution-mode cohort composition before changing "
                                "policy."
                            ),
                        )
                    )
    return findings


def _changed_findings(
    metrics: list[MetricRecord],
    *,
    evaluation_run_id: str,
) -> list[FindingRecord]:
    findings = []
    model_versions = metric_by_name(metrics, "model.version.unique_count")
    if model_versions and (to_decimal(model_versions.value) or Decimal("0")) > 1:
        findings.append(
            _finding(
                evaluation_run_id,
                "WHAT_CHANGED",
                "MODEL_VERSION_MIXED_SESSION",
                "INFO",
                "Forecast model versions changed during the session",
                "More than one forecast model version appears in the evaluated session.",
                (
                    "This is a deterministic lineage observation. Performance effects require "
                    "separate evidence."
                ),
                model_versions,
                current_value=model_versions.value,
                evidence_type="DETERMINISTIC_CHANGE",
                attribution_level="OBSERVATION",
                reason_codes=["MIXED_VERSION_SESSION"],
            )
        )
    feature_versions = metric_by_name(metrics, "feature_schema.version.unique_count")
    if feature_versions and (to_decimal(feature_versions.value) or Decimal("0")) > 1:
        findings.append(
            _finding(
                evaluation_run_id,
                "WHAT_CHANGED",
                "FEATURE_SCHEMA_MIXED_SESSION",
                "INFO",
                "Feature schema versions changed during the session",
                "More than one feature schema version appears in the evaluated session.",
                "This is a deterministic input-lineage observation.",
                feature_versions,
                current_value=feature_versions.value,
                evidence_type="DETERMINISTIC_CHANGE",
                attribution_level="OBSERVATION",
                reason_codes=["MIXED_FEATURE_SCHEMA_SESSION"],
            )
        )
    return findings


def _data_quality_findings(
    metrics: list[MetricRecord],
    *,
    evaluation_run_id: str,
) -> list[FindingRecord]:
    findings = []
    missing_lineage = metric_by_name(metrics, "data_quality.forecast_lineage_missing")
    if missing_lineage and (to_decimal(missing_lineage.value) or Decimal("0")) > 0:
        findings.append(
            _finding(
                evaluation_run_id,
                "DATA_QUALITY",
                "FORECAST_LINEAGE_MISSING",
                "WARNING",
                "Some forecast lineage fields are missing",
                "At least one forecast lacks model artifact, version, or feature schema lineage.",
                (
                    "Missing lineage lowers reliability and prevents strong model/version "
                    "attribution."
                ),
                missing_lineage,
                current_value=missing_lineage.value,
                evidence_type="OBSERVED",
                attribution_level="OBSERVATION",
                reason_codes=["MISSING_MODEL_OR_FEATURE_LINEAGE"],
            )
        )
    conflict = metric_by_name(metrics, "phase3n.approved_gt_proposed_conflicts")
    if conflict and (to_decimal(conflict.value) or Decimal("0")) > 0:
        findings.append(
            _finding(
                evaluation_run_id,
                "DATA_QUALITY",
                "PHASE3N_APPROVED_GT_PHASE3M_PROPOSED",
                "CRITICAL",
                "Phase 3N approved quantity exceeded Phase 3M proposal in source data",
                "At least one source record has approved contracts above proposed contracts.",
                "Treat this as source inconsistency until the upstream lineage is repaired.",
                conflict,
                current_value=conflict.value,
                evidence_type="OBSERVED",
                attribution_level="OBSERVATION",
                reason_codes=["SOURCE_CONFLICT"],
            )
        )
    return findings


def _watch_findings(
    metrics: list[MetricRecord],
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[FindingRecord]:
    watch = []
    for name in ("forecast.direction_accuracy", "forecast.brier_score"):
        metric = metric_by_name(metrics, name)
        if metric and 0 < metric.sample_size < settings.phase_3p_minimum_current_sample:
            watch.append(
                _finding(
                    evaluation_run_id,
                    "WATCH",
                    "SMALL_SAMPLE",
                    "INFO",
                    f"{name} has too little finalized evidence",
                    "The metric is tracked, but the finalized sample is too small for a verdict.",
                    (
                        "Small samples remain watch items and are not promoted to "
                        "worked/failed claims."
                    ),
                    metric,
                    current_value=metric.value,
                    status="WATCH",
                    reliability_grade="LOW",
                    attribution_level="OBSERVATION",
                    reason_codes=["BELOW_MINIMUM_CURRENT_SAMPLE"],
                )
            )
    pending_forecasts = metric_by_name(metrics, "coverage.forecasts.eligible")
    if pending_forecasts and pending_forecasts.pending_count:
        watch.append(
            _finding(
                evaluation_run_id,
                "WATCH",
                "PENDING_FORECAST_OUTCOMES",
                "INFO",
                "Some forecast outcomes are still pending",
                "Pending forecast outcomes are excluded from finalized metrics.",
                "The journal remains provisional while unresolved forecast labels can change.",
                pending_forecasts,
                current_value=str(pending_forecasts.pending_count),
                status="WATCH",
                reliability_grade=pending_forecasts.reliability_grade,
                reason_codes=["PENDING_OUTCOMES_EXCLUDED"],
            )
        )
    return watch


def _follow_ups(findings: list[FindingRecord]) -> list[dict[str, Any]]:
    output = []
    for finding in findings:
        follow_up_id = stable_phase_3p_id("follow_up", finding.finding_id)
        output.append(
            {
                "follow_up_id": follow_up_id,
                "title": f"Review {finding.title}",
                "type": "INVESTIGATION",
                "priority": finding.severity,
                "status": HUMAN_REVIEW_REQUIRED,
                "rationale": finding.concise_statement,
                "proposed_scope": "Review evidence and run a backtest or shadow experiment.",
                "success_metric": finding.primary_metric_record_id or "n/a",
                "minimum_sample_or_duration": "Human review before any system change.",
            }
        )
    return output


def _finding(
    evaluation_run_id: str,
    finding_type: str,
    subtype: str,
    severity: str,
    title: str,
    concise: str,
    detail: str,
    metric: MetricRecord,
    *,
    current_value: str | None = None,
    absolute_delta: str | None = None,
    effect_size: str | None = None,
    evidence_type: str = "OBSERVED",
    attribution_level: str = "OBSERVATION",
    reason_codes: list[str] | None = None,
    status: str = "SUPPORTED",
    reliability_grade: str | None = None,
    hypothesis: str | None = None,
) -> FindingRecord:
    finding_id = stable_phase_3p_id(
        "finding",
        evaluation_run_id,
        finding_type,
        subtype,
        metric.metric_record_id,
    )
    return FindingRecord(
        finding_id=finding_id,
        finding_type=finding_type,
        finding_subtype=subtype,
        severity=severity,
        status=status,
        title=title,
        concise_statement=concise,
        detailed_explanation=detail,
        primary_metric_record_id=metric.metric_record_id,
        sample_size=metric.sample_size,
        current_value=current_value,
        baseline=metric.baseline,
        absolute_delta=absolute_delta,
        relative_delta=_relative_delta(absolute_delta, metric.baseline.value),
        effect_size=effect_size,
        reliability_grade=reliability_grade or metric.reliability_grade,
        evidence_type=evidence_type,
        attribution_level=attribution_level,
        evidence_references=[
            {"reference_type": "METRIC_RECORD", "reference_id": metric.metric_record_id}
        ],
        reason_codes=reason_codes or [],
        hypothesis=hypothesis,
        recommended_follow_up_ids=[stable_phase_3p_id("follow_up", finding_id)],
        cohort=metric.cohort,
    )


def _supports_comparison(metric: MetricRecord, settings: Settings) -> bool:
    return (
        metric.sample_size >= settings.phase_3p_minimum_current_sample
        and metric.baseline.value is not None
        and not metric.baseline.fallback_used
        and metric.baseline.sample_size >= settings.phase_3p_minimum_baseline_sample
    )


def _effect_threshold(settings: Settings) -> Decimal:
    return Decimal(str(settings.phase_3p_minimum_practical_effect_size))


def _relative_delta(delta: str | None, baseline: str | None) -> str | None:
    delta_value = to_decimal(delta)
    baseline_value = to_decimal(baseline)
    if delta_value is None or baseline_value is None or baseline_value == 0:
        return None
    try:
        return decimal_string(delta_value / abs(baseline_value))
    except (DivisionByZero, InvalidOperation):
        return None


def _execution_modes(metrics: list[MetricRecord]) -> list[str]:
    return sorted(
        {
            str(metric.cohort["execution_mode"])
            for metric in metrics
            if metric.metric_name == "trade.net_pnl.total"
            and "execution_mode" in metric.cohort
            and metric.cohort["execution_mode"] != "NONE"
        }
    )
