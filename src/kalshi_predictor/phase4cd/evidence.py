from __future__ import annotations

import copy
import json
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, case, cast, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    CalibrationObservation,
    CanonicalEvaluation,
    CryptoFeatureLineage,
    EventCalibrationMetric,
    EvidenceExpansionPartition,
    ExecutableEdgeAttribution,
    PaperFill,
    PaperOrder,
    PaperPnl,
    ProspectiveCaptureAlert,
    ProspectiveHealthSnapshot,
    ProspectiveStatusLineage,
    ReplayDisposition,
    ResearchRun,
    ShadowDecision,
)
from kalshi_predictor.phase4cd.domain import (
    GUARDED_PAPER,
    HISTORICAL_REPLAY,
    SHADOW,
    evidence_level,
)

SUMMARY_CACHE_TTL_SECONDS = 30.0
SUMMARY_CACHE_MAX_ENTRIES = 2


@dataclass(frozen=True)
class _SummaryCacheEntry:
    stored_at: float
    payload: dict[str, Any]


class EvidenceSummaryCache:
    """Small process-local cache for read-only UI summaries only."""

    def __init__(self, *, max_entries: int = SUMMARY_CACHE_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _SummaryCacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    def get(
        self,
        key: str,
        *,
        now: float,
        ttl_seconds: float,
    ) -> tuple[dict[str, Any] | None, float, bool]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None, 0.0, False
            age = max(0.0, now - entry.stored_at)
            if age >= ttl_seconds or not _valid_summary_payload(entry.payload):
                malformed = age < ttl_seconds and not _valid_summary_payload(entry.payload)
                self._entries.pop(key, None)
                return None, age, malformed
            self._entries.move_to_end(key)
            return copy.deepcopy(entry.payload), age, False

    def put(self, key: str, payload: dict[str, Any], *, now: float) -> None:
        if not _valid_summary_payload(payload):
            raise ValueError("refusing malformed evidence summary cache entry")
        with self._lock:
            self._entries[key] = _SummaryCacheEntry(now, copy.deepcopy(payload))
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_SUMMARY_CACHE = EvidenceSummaryCache()


def cached_evidence_dashboard(
    session: Session,
    *,
    cache: EvidenceSummaryCache = _SUMMARY_CACHE,
    clock: Callable[[], float] = time.monotonic,
    ttl_seconds: float = SUMMARY_CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    now = clock()
    cached, age, malformed = cache.get("summary", now=now, ttl_seconds=ttl_seconds)
    if cached is not None:
        cached["cache_status"] = "HIT"
        cached["cache_age_seconds"] = round(age, 3)
        cached["query_duration_ms"] = 0.0
        return cached
    payload = evidence_dashboard(session)
    if malformed:
        payload["warnings"] = [*payload["warnings"], "MALFORMED_CACHE_ENTRY_REJECTED"]
        payload["partial_data"] = True
    payload["cache_status"] = "MISS"
    payload["cache_age_seconds"] = 0.0
    cache.put("summary", payload, now=now)
    return payload


def evidence_dashboard(session: Session, *, include_deep: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    lanes = {
        lane: _empty_lane_summary()
        for lane in (HISTORICAL_REPLAY, SHADOW, GUARDED_PAPER)
    }
    lane_rows = session.execute(
        select(
            CanonicalEvaluation.source_lane,
            func.count(CanonicalEvaluation.evaluation_id),
            func.count(func.distinct(CanonicalEvaluation.independent_event_id)),
            func.count(func.distinct(CanonicalEvaluation.model)),
            func.avg(cast(CanonicalEvaluation.brier_contribution, Numeric)),
            func.avg(cast(CanonicalEvaluation.log_loss_contribution, Numeric)),
            func.sum(cast(CanonicalEvaluation.realized_or_simulated_pnl, Numeric)),
        ).group_by(CanonicalEvaluation.source_lane)
    ).all()
    for lane, evaluated, events, models, brier, log_loss, pnl in lane_rows:
        lanes[lane] = {
            "evaluated": int(evaluated),
            "evaluated_contracts": int(evaluated),
            "independent_events": int(events),
            "models_evaluated": int(models),
            "brier": str(brier) if brier is not None else None,
            "log_loss": str(log_loss) if log_loss is not None else None,
            "net_executable_result": str(pnl or Decimal("0")),
        }
    operational_started = time.perf_counter()
    operational = session.execute(
        select(
            select(func.count()).select_from(PaperOrder).scalar_subquery(),
            select(func.count()).select_from(PaperFill).scalar_subquery(),
            select(func.count(func.distinct(PaperPnl.ticker)))
            .where(PaperPnl.settlement_result.is_not(None))
            .scalar_subquery(),
            select(func.sum(cast(PaperPnl.realized_pnl, Numeric)))
            .select_from(PaperPnl)
            .scalar_subquery(),
            select(func.count()).select_from(ShadowDecision).scalar_subquery(),
            select(
                func.coalesce(
                    func.sum(case((ShadowDecision.settlement_result.is_not(None), 1), else_=0)),
                    0,
                )
            )
            .select_from(ShadowDecision)
            .scalar_subquery(),
        )
    ).one()
    operational_duration_ms = round((time.perf_counter() - operational_started) * 1000, 3)
    (
        paper_orders,
        paper_fills,
        paper_settled,
        paper_pnl,
        shadow_decisions,
        shadow_settled,
    ) = operational
    historical = lanes[HISTORICAL_REPLAY]
    shadow = lanes[SHADOW]
    shadow["decisions"] = int(shadow_decisions or 0)
    shadow["settled"] = int(shadow_settled or 0)
    paper = lanes[GUARDED_PAPER]
    paper.update(
        {
            "orders": paper_orders,
            "fills": paper_fills,
            "settled": paper_settled,
            "realized_pnl": str(paper_pnl or Decimal("0")),
        }
    )
    latest_run = session.scalar(
        select(ResearchRun).order_by(ResearchRun.started_at.desc()).limit(1)
    )
    research = {"processed": 0, "calibration_observations": 0, "dispositions": {}}
    if latest_run:
        disposition_rows = session.execute(
            select(ReplayDisposition.disposition, func.count())
            .where(ReplayDisposition.run_id == latest_run.run_id)
            .group_by(ReplayDisposition.disposition)
        ).all()
        research = {
            "run_id": latest_run.run_id,
            "model": latest_run.model,
            "processed": latest_run.processed,
            "calibration_observations": int(
                session.scalar(
                    select(func.count())
                    .select_from(CalibrationObservation)
                    .where(CalibrationObservation.run_id == latest_run.run_id)
                )
                or 0
            ),
            "dispositions": {
                str(disposition): int(count) for disposition, count in disposition_rows
            },
        }
    lineage_counts = {
        str(verdict): int(count)
        for verdict, count in session.execute(
            select(CryptoFeatureLineage.verdict, func.count()).group_by(
                CryptoFeatureLineage.verdict
            )
        ).all()
    }
    latest_metric = session.scalar(
        select(EventCalibrationMetric).order_by(EventCalibrationMetric.created_at.desc()).limit(1)
    )
    event_calibration: dict[str, Any] = {"comparison_id": None, "models": {}}
    if latest_metric:
        metric_rows = session.execute(
            select(
                EventCalibrationMetric.model,
                func.count(),
                func.avg(cast(EventCalibrationMetric.brier, Numeric)),
                func.avg(cast(EventCalibrationMetric.log_loss, Numeric)),
            )
            .where(EventCalibrationMetric.comparison_id == latest_metric.comparison_id)
            .group_by(EventCalibrationMetric.model)
        ).all()
        event_calibration = {
            "comparison_id": latest_metric.comparison_id,
            "models": {
                model: {
                    "events": int(events),
                    "brier": str(brier),
                    "log_loss": str(log_loss),
                }
                for model, events, brier, log_loss in metric_rows
            },
        }
    latest_partition = session.scalar(
        select(EvidenceExpansionPartition)
        .order_by(EvidenceExpansionPartition.updated_at.desc())
        .limit(1)
    )
    expansion = (
        {
            "partition_id": latest_partition.partition_id,
            "status": latest_partition.status,
            "admitted": latest_partition.admitted,
            "independent_events": latest_partition.independent_events,
            "cohort_hash": latest_partition.cohort_hash,
        }
        if latest_partition
        else {}
    )
    latest_attribution = session.scalar(
        select(ExecutableEdgeAttribution)
        .order_by(ExecutableEdgeAttribution.created_at.desc())
        .limit(1)
    )
    attribution: dict[str, Any] = {"rows": 0, "positive_gross": 0, "positive_net": 0}
    if latest_attribution:
        attribution_rows = session.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(
                        case((cast(ExecutableEdgeAttribution.gross_edge, Numeric) > 0, 1), else_=0)
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case((cast(ExecutableEdgeAttribution.net_edge, Numeric) > 0, 1), else_=0)
                    ),
                    0,
                ),
            ).where(ExecutableEdgeAttribution.run_id == latest_attribution.run_id)
        ).one()
        attribution = {
            "run_id": latest_attribution.run_id,
            "rows": int(attribution_rows[0]),
            "positive_gross": int(attribution_rows[1]),
            "positive_net": int(attribution_rows[2]),
        }
    latest_health = session.scalar(
        select(ProspectiveHealthSnapshot)
        .order_by(ProspectiveHealthSnapshot.generated_at.desc())
        .limit(1)
    )
    health = (
        {
            "generated_at": latest_health.generated_at.isoformat(),
            "open_markets": latest_health.open_markets,
            "open_snapshots": latest_health.open_snapshots,
            "eligible_pairs": latest_health.eligible_pairs,
            "exact_crypto_links": latest_health.exact_crypto_links,
            "two_sided_books": latest_health.two_sided_books,
            "one_sided_books": latest_health.one_sided_books,
            "findings": json.loads(latest_health.findings_json),
        }
        if latest_health
        else {}
    )
    alert_counts = dict(
        session.execute(
            select(ProspectiveCaptureAlert.alert_type, func.count()).group_by(
                ProspectiveCaptureAlert.alert_type
            )
        ).all()
    )
    latest_status_lineage = session.scalar(
        select(ProspectiveStatusLineage)
        .order_by(ProspectiveStatusLineage.generated_at.desc())
        .limit(1)
    )
    status_lineage = (
        {
            "generated_at": latest_status_lineage.generated_at.isoformat(),
            "fetched_inventory": latest_status_lineage.fetched_inventory,
            "raw_active_or_open": latest_status_lineage.raw_active_or_open,
            "normalized_active_or_open": latest_status_lineage.normalized_active_or_open,
            "filtered_inactive": latest_status_lineage.filtered_inactive,
            "missing_snapshot": latest_status_lineage.missing_snapshot,
            "snapshot_active_or_open": latest_status_lineage.snapshot_active_or_open,
            "status_mismatch": latest_status_lineage.status_mismatch,
            "strict_pair_eligible": latest_status_lineage.strict_pair_eligible,
            "loss_counts": json.loads(latest_status_lineage.loss_counts_json),
            "sample_bundle_hash": latest_status_lineage.sample_bundle_hash,
        }
        if latest_status_lineage
        else {}
    )
    warnings = []
    if not health:
        warnings.append("PROSPECTIVE_HEALTH_UNAVAILABLE")
    if not status_lineage:
        warnings.append("PROSPECTIVE_STATUS_LINEAGE_UNAVAILABLE")
    generated_at = datetime.now(UTC).isoformat()
    freshness_times = [
        value for value in (
            health.get("generated_at"),
            status_lineage.get("generated_at"),
        ) if value
    ]
    latest_source_at = max(freshness_times) if freshness_times else None
    source_age_seconds = None
    if latest_source_at:
        source_age_seconds = max(
            0.0,
            (datetime.now(UTC) - datetime.fromisoformat(latest_source_at)).total_seconds(),
        )
    payload = {
        "generated_at": generated_at,
        "query_duration_ms": 0.0,
        "source_query_duration_ms": 0.0,
        "cache_status": "BYPASS" if include_deep else "MISS",
        "cache_age_seconds": 0.0,
        "data_freshness": {
            "status": "AVAILABLE" if freshness_times else "UNAVAILABLE",
            "latest_source_at": latest_source_at,
        },
        "read_model_source": "DATABASE",
        "source_watermark": latest_source_at,
        "source_age_seconds": (
            round(source_age_seconds, 3) if source_age_seconds is not None else None
        ),
        "fallback_used": False,
        "fallback_reason": None,
        "query_plan_class": "SQL_AGGREGATE_WITH_SETTLED_DISTINCT_SCAN",
        "cold_path_component_ms": operational_duration_ms,
        "cache_capacity": SUMMARY_CACHE_MAX_ENTRIES,
        "cache_ttl_seconds": SUMMARY_CACHE_TTL_SECONDS,
        "partial_data": bool(warnings),
        "warnings": warnings,
        "historical": historical,
        "shadow": shadow,
        "guarded_paper": paper,
        "total_independent_evaluated_events": sum(
            int(lanes[lane]["independent_events"])
            for lane in (HISTORICAL_REPLAY, SHADOW, GUARDED_PAPER)
        ),
        "performance_metrics_merged": False,
        "research_replay": research,
        "crypto_lineage": lineage_counts,
        "event_calibration": event_calibration,
        "evidence_expansion": expansion,
        "edge_attribution": attribution,
        "prospective_operational_health": health,
        "prospective_alerts": alert_counts,
        "prospective_status_lineage": status_lineage,
        "deep_diagnostics_included": include_deep,
    }
    if include_deep:
        from kalshi_predictor.phase4cd.operations import handoff_funnel
        from kalshi_predictor.phase4cd.prospective import prospective_status

        payload["prospective_paired_evidence"] = prospective_status(session)
        payload["prospective_handoff_funnel"] = handoff_funnel(session)
    duration = round((time.perf_counter() - started) * 1000, 3)
    payload["query_duration_ms"] = duration
    payload["source_query_duration_ms"] = duration
    return payload


def _valid_summary_payload(payload: Any) -> bool:
    required = {
        "generated_at",
        "historical",
        "shadow",
        "guarded_paper",
        "query_duration_ms",
        "deep_diagnostics_included",
        "warnings",
    }
    return (
        isinstance(payload, dict)
        and required.issubset(payload)
        and payload.get("deep_diagnostics_included") is False
        and isinstance(payload.get("warnings"), list)
    )


def _empty_lane_summary() -> dict[str, Any]:
    return {
        "evaluated": 0,
        "evaluated_contracts": 0,
        "independent_events": 0,
        "models_evaluated": 0,
        "brier": None,
        "log_loss": None,
        "net_executable_result": "0",
    }


def model_evidence(session: Session) -> list[dict[str, Any]]:
    rows = list(session.scalars(select(CanonicalEvaluation)))
    models: dict[str, list[CanonicalEvaluation]] = defaultdict(list)
    for row in rows:
        models[row.model].append(row)
    result = []
    for model, evidence in sorted(models.items()):
        lane_independent = {
            lane: len({row.independent_event_id for row in evidence if row.source_lane == lane})
            for lane in (HISTORICAL_REPLAY, SHADOW, GUARDED_PAPER)
        }
        independent_n = sum(lane_independent.values())
        result.append(
            {
                "model": model,
                "historical_independent_n": lane_independent[HISTORICAL_REPLAY],
                "shadow_independent_n": lane_independent[SHADOW],
                "paper_independent_n": lane_independent[GUARDED_PAPER],
                "brier": _mean(evidence, "brier_contribution"),
                "ece": _ece(evidence),
                "net_executable_result": str(
                    sum((Decimal(row.realized_or_simulated_pnl) for row in evidence), Decimal("0"))
                ),
                "evidence_level": evidence_level(independent_n).value,
                "certainty_claimed": False,
            }
        )
    return result


def proposed_model_weights(session: Session) -> dict[str, Any]:
    rows = model_evidence(session)
    quality = {
        row["model"]: max(Decimal("0"), Decimal("1") - Decimal(row["brier"] or "1"))
        * Decimal(
            row["historical_independent_n"]
            + row["shadow_independent_n"]
            + row["paper_independent_n"]
        ).sqrt()
        for row in rows
    }
    total = sum(quality.values(), Decimal("0"))
    proposed = {model: str(score / total) if total > 0 else "0" for model, score in quality.items()}
    return {
        "status": "PROPOSED_MODEL_WEIGHTS",
        "automatically_applied": False,
        "current_weights": {},
        "proposed_weights": proposed,
        "explanation": {
            model: "Evidence-weighted calibration support; production weight unchanged."
            for model in proposed
        },
    }


def _lane_summary(rows: list[CanonicalEvaluation]) -> dict[str, Any]:
    return {
        "evaluated": len(rows),
        "evaluated_contracts": len(rows),
        "independent_events": len({row.independent_event_id for row in rows}),
        "models_evaluated": len({row.model for row in rows}),
        "brier": _mean(rows, "brier_contribution"),
        "log_loss": _mean(rows, "log_loss_contribution"),
        "net_executable_result": str(
            sum((Decimal(row.realized_or_simulated_pnl) for row in rows), Decimal("0"))
        ),
    }


def _mean(rows: list[CanonicalEvaluation], field: str) -> str | None:
    if not rows:
        return None
    return str(sum((Decimal(getattr(row, field)) for row in rows), Decimal("0")) / len(rows))


def _ece(rows: list[CanonicalEvaluation], bins: int = 10) -> str | None:
    if not rows:
        return None
    grouped: dict[int, list[tuple[Decimal, int]]] = defaultdict(list)
    for row in rows:
        probability = Decimal(row.forecast_probability)
        outcome = 1 if row.settlement_result.strip().lower() == "yes" else 0
        index = min(int(probability * bins), bins - 1)
        grouped[index].append((probability, outcome))
    total = Decimal(len(rows))
    ece = Decimal("0")
    for values in grouped.values():
        confidence = sum((value[0] for value in values), Decimal("0")) / len(values)
        accuracy = Decimal(sum(value[1] for value in values)) / len(values)
        ece += Decimal(len(values)) / total * abs(confidence - accuracy)
    return str(ece)
