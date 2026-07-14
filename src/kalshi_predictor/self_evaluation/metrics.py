from __future__ import annotations

from collections import Counter
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import SelfEvaluationMetric
from kalshi_predictor.self_evaluation.contracts import (
    Baseline,
    MetricRecord,
    checksum_payload,
    decimal_string,
    reliability_grade,
    stable_phase_3p_id,
)
from kalshi_predictor.self_evaluation.dataset import EvaluationDataset
from kalshi_predictor.utils.decimals import to_decimal

METRIC_CATALOG: tuple[dict[str, str], ...] = (
    {
        "metric_name": "coverage.forecasts.eligible",
        "formula": "count(latest forecast records generated in session)",
        "unit": "forecasts",
        "cohort": "all forecasts",
        "version": "1.0.0",
    },
    {
        "metric_name": "forecast.direction_accuracy",
        "formula": "mean(direction_correct) over finalized forecasts",
        "unit": "ratio",
        "cohort": "finalized forecasts with direction labels",
        "version": "1.0.0",
    },
    {
        "metric_name": "forecast.brier_score",
        "formula": "mean((predicted_probability - binary_actual)^2)",
        "unit": "score",
        "cohort": "finalized binary probability forecasts",
        "version": "1.0.0",
    },
    {
        "metric_name": "trade.net_pnl.total",
        "formula": "sum(net_pnl) over finalized trades, grouped by execution_mode",
        "unit": "currency",
        "cohort": "finalized trades by execution_mode",
        "version": "1.0.0",
    },
    {
        "metric_name": "phase3n.action.count",
        "formula": "count(latest forecast records by phase_3n_action)",
        "unit": "forecasts",
        "cohort": "all forecasts",
        "version": "1.0.0",
    },
)


def build_metric_records(
    session: Session,
    dataset: EvaluationDataset,
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[MetricRecord]:
    metrics: list[MetricRecord] = []
    metrics.extend(
        _coverage_metrics(dataset, evaluation_run_id=evaluation_run_id, settings=settings)
    )
    metrics.extend(
        _forecast_metrics(dataset, evaluation_run_id=evaluation_run_id, settings=settings)
    )
    metrics.extend(
        _opportunity_metrics(dataset, evaluation_run_id=evaluation_run_id, settings=settings)
    )
    metrics.extend(
        _phase_3m_3n_metrics(dataset, evaluation_run_id=evaluation_run_id, settings=settings)
    )
    metrics.extend(
        _trade_metrics(dataset, evaluation_run_id=evaluation_run_id, settings=settings)
    )
    metrics.extend(
        _model_and_quality_metrics(
            dataset,
            evaluation_run_id=evaluation_run_id,
            settings=settings,
        )
    )
    return [
        _with_baseline(
            session,
            metric,
            trading_session_id=dataset.trading_session.trading_session_id,
            settings=settings,
        )
        for metric in metrics
    ]


def metric_payloads(metrics: list[MetricRecord]) -> list[dict[str, Any]]:
    return [metric.as_payload() for metric in metrics]


def metric_by_name(
    metrics: list[MetricRecord],
    metric_name: str,
    *,
    cohort_key: str | None = None,
    cohort_value: Any | None = None,
) -> MetricRecord | None:
    for metric in metrics:
        if metric.metric_name != metric_name:
            continue
        if cohort_key is not None and metric.cohort.get(cohort_key) != cohort_value:
            continue
        return metric
    return None


def _coverage_metrics(
    dataset: EvaluationDataset,
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[MetricRecord]:
    eligible_forecasts = len(dataset.forecast_rows)
    finalized_forecasts = len(dataset.finalized_forecasts)
    eligible_trades = len(dataset.trade_rows)
    finalized_trades = len(dataset.finalized_trades)
    market_linked = sum(1 for row in dataset.forecast_rows if row.market_memory_id)
    return [
        _metric(
            evaluation_run_id,
            "coverage.forecasts.eligible",
            "COVERAGE",
            "forecast_and_opportunity",
            Decimal(eligible_forecasts),
            "forecasts",
            eligible_forecasts,
            {"scope": "all"},
            settings=settings,
            finalized_count=finalized_forecasts,
            pending_count=len(dataset.pending_forecasts),
        ),
        _metric(
            evaluation_run_id,
            "coverage.trades.eligible",
            "COVERAGE",
            "trade_and_execution",
            Decimal(eligible_trades),
            "trades",
            eligible_trades,
            {"scope": "all"},
            settings=settings,
            finalized_count=finalized_trades,
            pending_count=len(dataset.open_trades),
        ),
        _metric(
            evaluation_run_id,
            "coverage.market_snapshot_link_rate",
            "COVERAGE",
            "data_quality",
            _ratio(market_linked, eligible_forecasts),
            "ratio",
            eligible_forecasts,
            {"scope": "forecast_memory"},
            settings=settings,
            finalized_count=market_linked,
            pending_count=max(0, eligible_forecasts - market_linked),
        ),
    ]


def _forecast_metrics(
    dataset: EvaluationDataset,
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[MetricRecord]:
    finalized = dataset.finalized_forecasts
    direction_values = [
        Decimal(int(row.direction_correct))
        for row in finalized
        if row.direction_correct is not None
    ]
    brier_values = [_brier(row) for row in finalized]
    brier_values = [value for value in brier_values if value is not None]
    no_trade_count = sum(1 for row in dataset.forecast_rows if row.decision_status == "NO_TRADE")
    risk_blocked = sum(
        1
        for row in dataset.forecast_rows
        if row.decision_status == "RISK_BLOCKED" or row.phase_3n_action == "BLOCK"
    )
    return [
        _metric(
            evaluation_run_id,
            "forecast.direction_accuracy",
            "FORECAST",
            "forecast_and_opportunity",
            _mean(direction_values),
            "ratio",
            len(direction_values),
            {"scope": "finalized"},
            settings=settings,
            finalized_count=len(direction_values),
            pending_count=len(dataset.pending_forecasts),
        ),
        _metric(
            evaluation_run_id,
            "forecast.brier_score",
            "FORECAST",
            "forecast_and_opportunity",
            _mean(brier_values),
            "score",
            len(brier_values),
            {"scope": "finalized_binary"},
            settings=settings,
            finalized_count=len(brier_values),
            pending_count=len(dataset.pending_forecasts),
        ),
        _metric(
            evaluation_run_id,
            "forecast.no_trade_count",
            "FORECAST",
            "forecast_and_opportunity",
            Decimal(no_trade_count),
            "forecasts",
            len(dataset.forecast_rows),
            {"decision_status": "NO_TRADE"},
            settings=settings,
            finalized_count=no_trade_count,
        ),
        _metric(
            evaluation_run_id,
            "forecast.risk_blocked_count",
            "FORECAST",
            "risk_and_sizing",
            Decimal(risk_blocked),
            "forecasts",
            len(dataset.forecast_rows),
            {"decision_status": "RISK_BLOCKED"},
            settings=settings,
            finalized_count=risk_blocked,
        ),
    ]


def _opportunity_metrics(
    dataset: EvaluationDataset,
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[MetricRecord]:
    scores = [
        value
        for value in (to_decimal(row.opportunity_score) for row in dataset.forecast_rows)
        if value is not None
    ]
    top_bucket = [value for value in scores if value >= Decimal("75")]
    return [
        _metric(
            evaluation_run_id,
            "opportunity.score.mean",
            "OPPORTUNITY",
            "forecast_and_opportunity",
            _mean(scores),
            "score",
            len(scores),
            {"scope": "eligible_forecasts"},
            settings=settings,
            finalized_count=len(scores),
        ),
        _metric(
            evaluation_run_id,
            "opportunity.top_bucket_count",
            "OPPORTUNITY",
            "forecast_and_opportunity",
            Decimal(len(top_bucket)),
            "forecasts",
            len(scores),
            {"opportunity_score": ">=75"},
            settings=settings,
            finalized_count=len(top_bucket),
        ),
    ]


def _phase_3m_3n_metrics(
    dataset: EvaluationDataset,
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[MetricRecord]:
    metrics: list[MetricRecord] = []
    tier_counts = Counter(row.phase_3m_tier or "UNKNOWN" for row in dataset.forecast_rows)
    action_counts = Counter(row.phase_3n_action or "UNKNOWN" for row in dataset.forecast_rows)
    reason_counts: Counter[str] = Counter()
    conflicts = 0
    for row in dataset.forecast_rows:
        for reason in _json_list(row.phase_3n_reason_codes_json):
            reason_counts[str(reason)] += 1
        if (
            row.phase_3m_proposed_contracts is not None
            and row.phase_3n_approved_contracts is not None
            and row.phase_3n_approved_contracts > row.phase_3m_proposed_contracts
        ):
            conflicts += 1
    for tier, count in sorted(tier_counts.items()):
        metrics.append(
            _metric(
                evaluation_run_id,
                "phase3m.tier.count",
                "POSITION_SIZING",
                "risk_and_sizing",
                Decimal(count),
                "forecasts",
                len(dataset.forecast_rows),
                {"phase_3m_tier": tier},
                settings=settings,
                finalized_count=count,
            )
        )
    for action, count in sorted(action_counts.items()):
        metrics.append(
            _metric(
                evaluation_run_id,
                "phase3n.action.count",
                "RISK",
                "risk_and_sizing",
                Decimal(count),
                "forecasts",
                len(dataset.forecast_rows),
                {"phase_3n_action": action},
                settings=settings,
                finalized_count=count,
            )
        )
    for reason, count in sorted(reason_counts.items()):
        metrics.append(
            _metric(
                evaluation_run_id,
                "phase3n.reason.count",
                "RISK",
                "risk_and_sizing",
                Decimal(count),
                "forecasts",
                len(dataset.forecast_rows),
                {"phase_3n_reason": reason},
                settings=settings,
                finalized_count=count,
            )
        )
    metrics.append(
        _metric(
            evaluation_run_id,
            "phase3n.approved_gt_proposed_conflicts",
            "DATA_QUALITY",
            "data_quality",
            Decimal(conflicts),
            "conflicts",
            len(dataset.forecast_rows),
            {"scope": "phase3m_phase3n_validation"},
            settings=settings,
            finalized_count=conflicts,
        )
    )
    return metrics


def _trade_metrics(
    dataset: EvaluationDataset,
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[MetricRecord]:
    metrics: list[MetricRecord] = []
    modes = sorted({row.execution_mode or "UNKNOWN" for row in dataset.trade_rows}) or ["NONE"]
    for mode in modes:
        rows = [row for row in dataset.trade_rows if (row.execution_mode or "UNKNOWN") == mode]
        finalized = [row for row in rows if row in dataset.finalized_trades]
        net_values = [
            value for value in (to_decimal(row.net_pnl) for row in finalized) if value is not None
        ]
        gross_values = [
            value for value in (to_decimal(row.gross_pnl) for row in finalized) if value is not None
        ]
        cost_values = [
            _trade_cost(row)
            for row in finalized
            if _trade_cost(row) is not None
        ]
        wins = [value for value in net_values if value > 0]
        metrics.extend(
            [
                _metric(
                    evaluation_run_id,
                    "trade.count",
                    "TRADE",
                    "trade_and_execution",
                    Decimal(len(rows)),
                    "trades",
                    len(rows),
                    {"execution_mode": mode},
                    settings=settings,
                    finalized_count=len(finalized),
                    pending_count=max(0, len(rows) - len(finalized)),
                ),
                _metric(
                    evaluation_run_id,
                    "trade.net_pnl.total",
                    "TRADE",
                    "trade_and_execution",
                    sum(net_values, Decimal("0")) if net_values else None,
                    "currency",
                    len(net_values),
                    {"execution_mode": mode},
                    settings=settings,
                    finalized_count=len(net_values),
                    pending_count=max(0, len(rows) - len(finalized)),
                ),
                _metric(
                    evaluation_run_id,
                    "trade.net_pnl.mean",
                    "TRADE",
                    "trade_and_execution",
                    _mean(net_values),
                    "currency",
                    len(net_values),
                    {"execution_mode": mode},
                    settings=settings,
                    finalized_count=len(net_values),
                ),
                _metric(
                    evaluation_run_id,
                    "trade.win_rate",
                    "TRADE",
                    "trade_and_execution",
                    _ratio(len(wins), len(net_values)),
                    "ratio",
                    len(net_values),
                    {"execution_mode": mode},
                    settings=settings,
                    finalized_count=len(net_values),
                ),
                _metric(
                    evaluation_run_id,
                    "trade.gross_pnl.total",
                    "TRADE",
                    "trade_and_execution",
                    sum(gross_values, Decimal("0")) if gross_values else None,
                    "currency",
                    len(gross_values),
                    {"execution_mode": mode},
                    settings=settings,
                    finalized_count=len(gross_values),
                ),
                _metric(
                    evaluation_run_id,
                    "trade.cost.total",
                    "TRADE",
                    "trade_and_execution",
                    sum(cost_values, Decimal("0")) if cost_values else None,
                    "currency",
                    len(cost_values),
                    {"execution_mode": mode},
                    settings=settings,
                    finalized_count=len(cost_values),
                ),
            ]
        )
    return metrics


def _model_and_quality_metrics(
    dataset: EvaluationDataset,
    *,
    evaluation_run_id: str,
    settings: Settings,
) -> list[MetricRecord]:
    versions = sorted(
        {
            row.primary_model_version
            for row in dataset.forecast_rows
            if row.primary_model_version
        }
    )
    feature_versions = sorted(
        {
            row.feature_schema_version
            for row in dataset.forecast_rows
            if row.feature_schema_version
        }
    )
    missing_lineage = sum(
        1
        for row in dataset.forecast_rows
        if not row.primary_model_version
        or not row.primary_model_artifact_hash
        or not row.feature_schema_version
    )
    return [
        _metric(
            evaluation_run_id,
            "model.version.unique_count",
            "CHANGE",
            "model_and_version",
            Decimal(len(versions)),
            "versions",
            len(dataset.forecast_rows),
            {"versions": ",".join(versions) if versions else "UNKNOWN"},
            settings=settings,
            finalized_count=len(versions),
        ),
        _metric(
            evaluation_run_id,
            "feature_schema.version.unique_count",
            "CHANGE",
            "model_and_version",
            Decimal(len(feature_versions)),
            "versions",
            len(dataset.forecast_rows),
            {"versions": ",".join(feature_versions) if feature_versions else "UNKNOWN"},
            settings=settings,
            finalized_count=len(feature_versions),
        ),
        _metric(
            evaluation_run_id,
            "data_quality.forecast_lineage_missing",
            "DATA_QUALITY",
            "data_quality",
            Decimal(missing_lineage),
            "forecasts",
            len(dataset.forecast_rows),
            {"scope": "forecast_lineage"},
            settings=settings,
            finalized_count=missing_lineage,
        ),
    ]


def _with_baseline(
    session: Session,
    metric: MetricRecord,
    *,
    trading_session_id: str,
    settings: Settings,
) -> MetricRecord:
    values: list[Decimal] = []
    sample_size = 0
    cohort_hash = checksum_payload(metric.cohort)
    rows = session.scalars(
        select(SelfEvaluationMetric)
        .where(SelfEvaluationMetric.metric_name == metric.metric_name)
        .where(SelfEvaluationMetric.trading_session_id != trading_session_id)
        .order_by(desc(SelfEvaluationMetric.created_at))
        .limit(_largest_baseline_window(settings))
    )
    for row in rows:
        if checksum_payload(decode_json(row.cohort_json)) != cohort_hash:
            continue
        value = to_decimal(row.value)
        if value is None:
            continue
        values.append(value)
        sample_size += row.sample_size
    if not values:
        baseline = Baseline("NONE", sample_size=0, value=None, window=None, fallback_used=True)
    else:
        baseline = Baseline(
            "TRAILING_COMPLETED_SESSIONS",
            sample_size=sample_size,
            value=decimal_string(_mean(values)),
            window=f"{len(values)} stored metric records",
            fallback_used=sample_size < settings.phase_3p_minimum_baseline_sample,
        )
    return MetricRecord(**{**metric.__dict__, "baseline": baseline})


def _metric(
    evaluation_run_id: str,
    name: str,
    metric_type: str,
    section: str,
    value: Decimal | None,
    unit: str,
    sample_size: int,
    cohort: dict[str, Any],
    *,
    settings: Settings,
    finalized_count: int = 0,
    pending_count: int = 0,
) -> MetricRecord:
    metric_id = stable_phase_3p_id("metric", evaluation_run_id, name, checksum_payload(cohort))
    return MetricRecord(
        metric_record_id=metric_id,
        metric_name=name,
        metric_type=metric_type,
        section=section,
        value=decimal_string(value),
        unit=unit,
        sample_size=sample_size,
        finalized_count=finalized_count,
        pending_count=pending_count,
        cohort=cohort,
        reliability_grade=reliability_grade(
            sample_size,
            minimum_current_sample=settings.phase_3p_minimum_current_sample,
        ),
        evidence_references=[{"reference_type": "METRIC_DEFINITION", "reference_id": name}],
    )


def _brier(row: Any) -> Decimal | None:
    stored = to_decimal(row.brier_component)
    if stored is not None:
        return stored
    probability = to_decimal(row.predicted_probability)
    actual = _binary_actual(row.actual_value, row.outcome_class)
    if probability is None or actual is None:
        return None
    return (probability - actual) * (probability - actual)


def _binary_actual(actual_value: str | None, outcome_class: str | None) -> Decimal | None:
    value = to_decimal(actual_value)
    if value is not None and value in {Decimal("0"), Decimal("1")}:
        return value
    normalized = (outcome_class or "").strip().lower()
    if normalized in {"yes", "true", "win", "won", "up", "1"}:
        return Decimal("1")
    if normalized in {"no", "false", "loss", "lost", "down", "0"}:
        return Decimal("0")
    return None


def _trade_cost(row: Any) -> Decimal | None:
    values = [
        to_decimal(row.total_cost),
        to_decimal(row.commission),
        to_decimal(row.exchange_fees),
        to_decimal(row.borrow_or_carry_cost),
    ]
    total = sum((value for value in values if value is not None), Decimal("0"))
    return total if any(value is not None for value in values) else None


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0:
        return None
    try:
        return Decimal(numerator) / Decimal(denominator)
    except (DivisionByZero, InvalidOperation):
        return None


def _json_list(value: str | None) -> list[Any]:
    decoded = decode_json(value)
    if isinstance(decoded, dict):
        for key in ("reason_codes", "reasons", "codes"):
            item = decoded.get(key)
            if isinstance(item, list):
                return item
    return []


def _largest_baseline_window(settings: Settings) -> int:
    values = []
    for item in settings.phase_3p_baseline_completed_session_windows.split(","):
        try:
            values.append(int(item.strip()))
        except ValueError:
            continue
    return max(values or [60])
