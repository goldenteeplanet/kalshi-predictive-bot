from __future__ import annotations

import csv
import io
import subprocess
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.reports import advanced_risk_card
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import database_url_from_settings, redact_database_url
from kalshi_predictor.data.maintenance import database_status_card
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    FeatureDiscoveryRun,
    Forecast,
    ForecastMemory,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    PaperPnl,
    PaperPosition,
    ReadinessDecisionRecord,
    RlRun,
    SelfEvaluationJournal,
    Settlement,
    SyntheticMarketRun,
    SystemCertificationRun,
    TradeMemory,
)
from kalshi_predictor.feature_discovery.repository import feature_discovery_status
from kalshi_predictor.institutional_dashboard.contracts import (
    API_SCHEMA_VERSION,
    COMPLETENESS_COMPLETE,
    COMPLETENESS_NOT_APPLICABLE,
    COMPLETENESS_PARTIAL,
    COMPLETENESS_UNAVAILABLE,
    FRESHNESS_AGING,
    FRESHNESS_FRESH,
    FRESHNESS_NOT_APPLICABLE,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_BLOCKED_BY_DEPENDENCY,
    LIFECYCLE_DISABLED,
    LIFECYCLE_NOT_APPLICABLE,
    LIFECYCLE_UNINITIALIZED,
    LIFECYCLE_WAITING_FOR_OUTCOMES,
    METRIC_CATALOG,
    MODE_DISABLED,
    PANEL_DEFINITIONS,
    SNAPSHOT_SCHEMA_VERSION,
    DashboardConfig,
    SourceWatermark,
    canonical_json,
    decimal_or_na,
    query_hash,
    stable_id,
)
from kalshi_predictor.live_readiness.service import live_readiness_panel
from kalshi_predictor.memory.reports import memory_health
from kalshi_predictor.opportunities.market_identity import (
    market_identity_fields,
    verify_market_identity,
)
from kalshi_predictor.personal_trader.service import build_personal_trade_brief
from kalshi_predictor.reinforcement_learning.repository import rl_status
from kalshi_predictor.synthetic_markets.repository import synthetic_markets_status
from kalshi_predictor.system_certification.reports import system_certification_card
from kalshi_predictor.ui.market_display import classify_market_category
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now
from kalshi_predictor.workstation.repository import (
    model_performance_rows,
    portfolio_summary,
    position_rows,
)

READ_ONLY_BOUNDARY = {
    "read_only": True,
    "allow_exchange_write_endpoints": False,
    "allow_order_actions": False,
    "allow_position_sizing_changes": False,
    "allow_risk_overrides": False,
    "allow_model_promotion": False,
    "allow_feature_promotion": False,
    "allow_policy_promotion": False,
    "allow_settlement_changes": False,
    "expose_exchange_credentials_to_browser": False,
}


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    source_name: str
    source_type: str
    model: Any
    attempt_column: Any
    success_column: Any
    watermark_column: Any
    required: bool
    producer_stage: str
    next_command: str | None
    enabled_setting: str | None = None
    mode_setting: str | None = None


def config_from_settings(settings: Settings | None = None) -> DashboardConfig:
    resolved = settings or get_settings()
    mode = resolved.phase_3t_mode
    if not resolved.phase_3t_institutional_dashboard_enabled:
        mode = MODE_DISABLED
    config = DashboardConfig(
        enabled=resolved.phase_3t_institutional_dashboard_enabled,
        mode=mode,
        dashboard_definition_version=resolved.phase_3t_dashboard_definition_version,
        panel_registry_version=resolved.phase_3t_panel_registry_version,
        metric_catalog_version=resolved.phase_3t_metric_catalog_version,
        snapshot_validity_seconds=resolved.phase_3t_snapshot_validity_seconds,
        fresh_after_seconds=resolved.phase_3t_fresh_after_seconds,
        stale_after_seconds=resolved.phase_3t_stale_after_seconds,
        max_source_skew_seconds=resolved.phase_3t_max_source_skew_seconds,
        max_rows_per_panel=resolved.phase_3t_max_rows_per_panel,
        display_timezone=resolved.phase_3x_timezone,
    )
    config.validate()
    return config


def build_dashboard_snapshot(
    session: Session,
    *,
    settings: Settings | None = None,
    filters: dict[str, Any] | None = None,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    config = config_from_settings(resolved_settings)
    generated_at = utc_now()
    effective_as_of = parse_datetime(as_of) or generated_at
    filters = _effective_filters(filters)
    runtime = _runtime_context(session, settings=resolved_settings, config=config)
    watermarks = _source_watermarks(
        session,
        config=config,
        settings=resolved_settings,
        generated_at=generated_at,
        runtime=runtime,
    )
    skew = _cross_panel_skew(watermarks, config=config)
    freshness_status = _aggregate_freshness(watermarks)
    completeness_status = _aggregate_completeness(watermarks)
    warnings = _snapshot_warnings(
        config=config,
        watermarks=watermarks,
        freshness_status=freshness_status,
        completeness_status=completeness_status,
        skew=skew,
    )
    panel_registry = _panel_registry(config)
    panels = _panels(
        session,
        settings=resolved_settings,
        config=config,
        watermarks=watermarks,
        filters=filters,
    )
    snapshot_id = stable_id(
        generated_at.isoformat(),
        config.mode,
        canonical_json(filters),
        _watermark_signature(watermarks),
    )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "generated_at": generated_at.isoformat(),
        "requested_as_of": parse_datetime(as_of).isoformat() if as_of is not None else None,
        "effective_as_of": effective_as_of.isoformat(),
        "valid_until": (
            generated_at.timestamp() + config.snapshot_validity_seconds
        ),
        "environment": resolved_settings.kalshi_env,
        "execution_mode": _execution_mode(resolved_settings),
        "dashboard_mode": config.mode.upper(),
        "phase_3t_gate": _phase_3t_gate(
            config=config,
            watermarks=watermarks,
            freshness_status=freshness_status,
            completeness_status=completeness_status,
            skew=skew,
        ),
        "time_mode": _time_mode(config),
        "consistency_mode": skew["consistency_status"],
        "freshness_status": freshness_status,
        "completeness_status": completeness_status,
        "cross_panel_skew_seconds": skew["value_seconds"],
        "cross_panel_skew": skew,
        "effective_filters": filters,
        "runtime_context": runtime,
        "producer_chain": _producer_chain(watermarks),
        "source_watermarks": [watermark.as_payload() for watermark in watermarks],
        "source_statuses": [watermark.as_payload() for watermark in watermarks],
        "authorization_redactions": [],
        "warnings": warnings,
        "read_only_boundary": READ_ONLY_BOUNDARY,
        "dashboard_definition_version": config.dashboard_definition_version,
        "panel_registry_version": config.panel_registry_version,
        "metric_catalog_version": config.metric_catalog_version,
        "panel_registry": panel_registry,
        "metric_catalog": list(METRIC_CATALOG),
        "panels": panels,
        "reconciliation": _reconciliation(session, panels),
    }


def dashboard_panel_response(
    snapshot: dict[str, Any],
    *,
    panel_id: str,
    data: Any,
    filters: dict[str, Any] | None = None,
    sort: dict[str, Any] | None = None,
    pagination: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "request_id": str(uuid.uuid4()),
        "query_hash": query_hash(
            {
                "panel_id": panel_id,
                "filters": filters or {},
                "sort": sort or {},
                "pagination": pagination or {},
            }
        ),
        "dashboard_snapshot_id": snapshot["snapshot_id"],
        "generated_at": utc_now().isoformat(),
        "panel_as_of": snapshot["effective_as_of"],
        "effective_filters": filters or snapshot["effective_filters"],
        "sort": sort or {},
        "pagination": pagination,
        "source_watermarks": snapshot["source_watermarks"],
        "freshness_status": snapshot["freshness_status"],
        "completeness_status": snapshot["completeness_status"],
        "authorization_redactions": snapshot["authorization_redactions"],
        "warnings": list(snapshot["warnings"]) + list(warnings or []),
        "data": data,
    }


def panel_data(snapshot: dict[str, Any], panel_id: str) -> Any:
    return snapshot["panels"].get(panel_id, {"status": "NOT_AVAILABLE"})


def export_snapshot_csv(snapshot: dict[str, Any]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["panel_id", "title", "freshness_status", "completeness_status", "rows"])
    for panel in snapshot["panel_registry"]:
        data = panel_data(snapshot, panel["panel_id"])
        rows = len(data) if isinstance(data, list) else 1
        writer.writerow(
            [
                sanitize_csv_cell(panel["panel_id"]),
                sanitize_csv_cell(panel["title"]),
                sanitize_csv_cell(snapshot["freshness_status"]),
                sanitize_csv_cell(snapshot["completeness_status"]),
                rows,
            ]
        )
    return output.getvalue()


def sanitize_csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def _panels(
    session: Session,
    *,
    settings: Settings,
    config: DashboardConfig,
    watermarks: list[SourceWatermark],
    filters: dict[str, Any],
) -> dict[str, Any]:
    portfolio = portfolio_summary(session)
    models = model_performance_rows(session)
    risk = advanced_risk_card(session, settings=settings)
    market_rows = _market_heatmap(session, limit=config.max_rows_per_panel)
    opportunities = _opportunity_rows(
        session,
        limit=config.max_rows_per_panel,
        filters=filters,
        settings=settings,
    )
    personal_trader = build_personal_trade_brief(
        session,
        settings=settings,
        persist=False,
    )
    positions = position_rows(session, limit=config.max_rows_per_panel)
    trade_rows = _trade_rows(session, limit=config.max_rows_per_panel)
    research = _research_layers(session)
    return {
        "environment_status": {
            "mode": config.mode.upper(),
            "environment": settings.kalshi_env,
            "execution_mode": _execution_mode(settings),
            "feed_health": _aggregate_freshness(watermarks),
            "source_count": len(watermarks),
            "read_only": True,
            "paper_is_live": False,
            "synthetic_is_tradable": False,
        },
        "kpi_ribbon": {
            "net_pnl": _known_or_na(portfolio.get("total_pnl")),
            "realized_pnl": _known_or_na(portfolio.get("realized_pnl")),
            "unrealized_pnl": _known_or_na(portfolio.get("unrealized_pnl")),
            "open_positions": portfolio.get("open_positions"),
            "open_orders": portfolio.get("open_orders"),
            "risk_decisions": risk.get("decision_count"),
            "risk_blocked": risk.get("block_count"),
            "warnings": _warning_count(watermarks),
        },
        "market_heatmap": market_rows,
        "opportunity_scanner": opportunities,
        "personal_trader_brief": {
            "brief_id": personal_trader["brief_id"],
            "execution_mode": personal_trader["execution_mode"],
            "recommended_count": personal_trader["summary"]["recommended_count"],
            "eligible_count": personal_trader["summary"]["eligible_count"],
            "no_trade": personal_trader["no_trade"],
            "top_recommendations": [
                {
                    "rank": row["slate_rank"],
                    "ticker": row["market"]["market_ticker"],
                    "side": row["market"]["side"],
                    "approved_quantity": row["economics"]["approved_quantity"],
                    "risk_adjusted_ev_lcb_total": row["economics"]["risk_adjusted_ev_lcb_total"],
                }
                for row in personal_trader["recommendations"][:5]
            ],
            "read_only": True,
        },
        "model_matrix": _model_matrix(models),
        "exposure_maps": {
            "positions": positions,
            "category_allocation": portfolio.get("category_allocation", []),
            "total_exposure": _known_or_na(portfolio.get("total_exposure")),
            "lineage": "paper_positions via portfolio_summary",
        },
        "risk_waterfall": {
            **risk,
            "recent_decisions": _risk_decisions(session, limit=config.max_rows_per_panel),
            "phase_3m_authority": "PRESERVED",
            "phase_3n_authority": "FINAL",
        },
        "live_readiness": live_readiness_panel(session, settings=settings),
        "system_certification": {
            **system_certification_card(session, settings=settings),
            "read_only": True,
            "allow_live_execution": False,
            "allow_order_create": False,
        },
        "trade_blotter": trade_rows,
        "system_health": {
            "database": database_status_card(session, settings=settings),
            "memory": memory_health(session, settings=settings),
            "sources": [watermark.as_payload() for watermark in watermarks],
        },
        "research_layers": research,
    }


def _source_watermarks(
    session: Session,
    *,
    config: DashboardConfig,
    settings: Settings,
    generated_at: datetime,
    runtime: dict[str, Any],
) -> list[SourceWatermark]:
    return [
        _watermark(
            session,
            spec=spec,
            config=config,
            settings=settings,
            generated_at=generated_at,
            runtime=runtime,
        )
        for spec in _source_specs()
    ]


def _watermark(
    session: Session,
    *,
    spec: SourceSpec,
    config: DashboardConfig,
    settings: Settings,
    generated_at: datetime,
    runtime: dict[str, Any],
) -> SourceWatermark:
    last_attempt = parse_datetime(session.scalar(select(func.max(spec.attempt_column))))
    last_success = parse_datetime(session.scalar(select(func.max(spec.success_column))))
    latest = parse_datetime(session.scalar(select(func.max(spec.watermark_column))))
    row_count = _row_count(session, spec.model)
    enabled = _source_enabled(settings, spec)
    lifecycle = _lifecycle_state(
        session,
        spec=spec,
        enabled=enabled,
        row_count=row_count,
    )
    freshness = _source_freshness_status(
        latest,
        spec=spec,
        enabled=enabled,
        lifecycle=lifecycle,
        generated_at=generated_at,
        config=config,
    )
    completeness = _source_completeness_status(
        spec=spec,
        enabled=enabled,
        lifecycle=lifecycle,
        row_count=row_count,
    )
    error = _source_error(session, spec=spec, enabled=enabled, lifecycle=lifecycle)
    warning = _source_warning(
        spec=spec,
        freshness=freshness,
        completeness=completeness,
        lifecycle=lifecycle,
        error=error,
    )
    return SourceWatermark(
        source_id=spec.source_id,
        source_name=spec.source_name,
        source_type=spec.source_type,
        required=spec.required,
        requirement_state="REQUIRED" if spec.required else "OPTIONAL",
        enabled=enabled,
        lifecycle_state=lifecycle,
        last_attempt_at=last_attempt.isoformat() if last_attempt else None,
        last_success_at=last_success.isoformat() if last_success else None,
        data_watermark=latest.isoformat() if latest else None,
        latest_at=latest.isoformat() if latest else None,
        row_count=row_count,
        freshness_threshold_seconds=config.stale_after_seconds,
        freshness_status=freshness,
        completeness_status=completeness,
        error=error,
        warning=warning,
        database_fingerprint=runtime["database_fingerprint"],
        git_commit=runtime["git_commit"],
        producer_stage=spec.producer_stage,
        next_command=spec.next_command,
    )


def _source_freshness_status(
    latest: datetime | None,
    *,
    spec: SourceSpec,
    enabled: bool,
    lifecycle: str,
    generated_at: datetime,
    config: DashboardConfig,
) -> str:
    if not enabled and not spec.required:
        return FRESHNESS_NOT_APPLICABLE
    if lifecycle in {
        LIFECYCLE_DISABLED,
        LIFECYCLE_NOT_APPLICABLE,
    }:
        return FRESHNESS_NOT_APPLICABLE
    if lifecycle == LIFECYCLE_WAITING_FOR_OUTCOMES and not spec.required:
        return FRESHNESS_NOT_APPLICABLE
    if latest is None:
        return FRESHNESS_UNKNOWN
    age = max(Decimal("0"), Decimal(str((generated_at - latest).total_seconds())))
    if age > Decimal(config.stale_after_seconds):
        return FRESHNESS_STALE
    if age > Decimal(config.fresh_after_seconds):
        return FRESHNESS_AGING
    return FRESHNESS_FRESH


def _aggregate_freshness(watermarks: list[SourceWatermark]) -> str:
    required = [
        item
        for item in watermarks
        if item.required and item.freshness_status != FRESHNESS_NOT_APPLICABLE
    ]
    if not required:
        return FRESHNESS_NOT_APPLICABLE
    if any(item.freshness_status == FRESHNESS_STALE for item in required):
        return FRESHNESS_STALE
    if any(item.freshness_status == FRESHNESS_UNKNOWN for item in required):
        return FRESHNESS_UNKNOWN
    if any(item.freshness_status == FRESHNESS_AGING for item in required):
        return FRESHNESS_AGING
    return FRESHNESS_FRESH


def _aggregate_completeness(watermarks: list[SourceWatermark]) -> str:
    required = [
        item
        for item in watermarks
        if item.required and item.completeness_status != COMPLETENESS_NOT_APPLICABLE
    ]
    if not required:
        return COMPLETENESS_NOT_APPLICABLE
    if any(item.completeness_status == COMPLETENESS_UNAVAILABLE for item in required):
        return COMPLETENESS_UNAVAILABLE
    if any(item.completeness_status == COMPLETENESS_PARTIAL for item in required):
        return COMPLETENESS_PARTIAL
    return COMPLETENESS_COMPLETE


def _snapshot_warnings(
    *,
    config: DashboardConfig,
    watermarks: list[SourceWatermark],
    freshness_status: str,
    completeness_status: str,
    skew: dict[str, Any],
) -> list[str]:
    warnings = [
        item.warning
        for item in watermarks
        if item.warning and (item.required or item.enabled)
    ]
    if config.mode == MODE_DISABLED:
        warnings.append("Phase 3T is disabled; snapshot is read-only diagnostic output.")
    if freshness_status in {FRESHNESS_STALE, FRESHNESS_UNKNOWN}:
        warnings.append("Required dashboard sources are stale or unknown.")
    if completeness_status not in {COMPLETENESS_COMPLETE, COMPLETENESS_NOT_APPLICABLE}:
        warnings.append("Required dashboard sources are partial or unavailable.")
    if skew["status"] == "DEGRADED":
        warnings.append(
            "Cross-panel skew exceeds "
            f"{skew['threshold_seconds']} seconds between "
            f"{skew['oldest_source_id']} and {skew['newest_source_id']}."
        )
    return warnings


def _source_specs() -> tuple[SourceSpec, ...]:
    return (
        SourceSpec(
            source_id="market_state",
            source_name="Market ingestion",
            source_type="market_data",
            model=MarketSnapshot,
            attempt_column=MarketSnapshot.captured_at,
            success_column=MarketSnapshot.captured_at,
            watermark_column=MarketSnapshot.captured_at,
            required=True,
            producer_stage="market ingestion",
            next_command="kalshi-bot phase3ay-health-refresh --all-markets --duration-hours 1",
        ),
        SourceSpec(
            source_id="forecast_state",
            source_name="Forecasts",
            source_type="forecast",
            model=Forecast,
            attempt_column=Forecast.forecasted_at,
            success_column=Forecast.forecasted_at,
            watermark_column=Forecast.forecasted_at,
            required=True,
            producer_stage="forecasts",
            next_command="kalshi-bot autopilot-once",
        ),
        SourceSpec(
            source_id="opportunity_state",
            source_name="Opportunity rankings",
            source_type="opportunity",
            model=MarketRanking,
            attempt_column=MarketRanking.ranked_at,
            success_column=MarketRanking.ranked_at,
            watermark_column=MarketRanking.ranked_at,
            required=True,
            producer_stage="opportunities",
            next_command="kalshi-bot autopilot-once",
        ),
        SourceSpec(
            source_id="phase_3n_risk",
            source_name="Phase 3N risk decisions",
            source_type="risk",
            model=AdvancedRiskDecisionLog,
            attempt_column=AdvancedRiskDecisionLog.created_at,
            success_column=AdvancedRiskDecisionLog.decision_timestamp,
            watermark_column=AdvancedRiskDecisionLog.decision_timestamp,
            required=True,
            producer_stage="Phase 3N risk",
            next_command="kalshi-bot autopilot-once",
        ),
        SourceSpec(
            source_id="trade_state",
            source_name="Paper trades",
            source_type="trade",
            model=PaperOrder,
            attempt_column=PaperOrder.created_at,
            success_column=PaperOrder.created_at,
            watermark_column=PaperOrder.created_at,
            required=True,
            producer_stage="paper trades",
            next_command="kalshi-bot autopilot-once",
        ),
        SourceSpec(
            source_id="settlement_state",
            source_name="Exact-ticker settlements",
            source_type="settlement",
            model=Settlement,
            attempt_column=Settlement.updated_at,
            success_column=Settlement.updated_at,
            watermark_column=Settlement.updated_at,
            required=False,
            producer_stage="settlement",
            next_command=(
                "kalshi-bot sync-settlements --lookback-days 90 --limit 200 --max-pages 10"
            ),
        ),
        SourceSpec(
            source_id="trade_pnl_state",
            source_name="Paper P&L realization",
            source_type="trade",
            model=PaperPnl,
            attempt_column=PaperPnl.calculated_at,
            success_column=PaperPnl.calculated_at,
            watermark_column=PaperPnl.calculated_at,
            required=False,
            producer_stage="paper P&L realization",
            next_command="kalshi-bot phase3aa-realize --dry-run --no-sync-settlements",
        ),
        SourceSpec(
            source_id="phase_3o_market_memory",
            source_name="Phase 3O market memory",
            source_type="memory",
            model=ForecastMemory,
            attempt_column=ForecastMemory.recorded_at,
            success_column=ForecastMemory.recorded_at,
            watermark_column=ForecastMemory.recorded_at,
            required=True,
            producer_stage="Phase 3O memory",
            next_command="kalshi-bot memory-backfill",
            enabled_setting="phase_3o_market_memory_enabled",
            mode_setting="phase_3o_market_memory_mode",
        ),
        SourceSpec(
            source_id="trade_memory",
            source_name="Trade memory",
            source_type="memory",
            model=TradeMemory,
            attempt_column=TradeMemory.recorded_at,
            success_column=TradeMemory.recorded_at,
            watermark_column=TradeMemory.recorded_at,
            required=False,
            producer_stage="trade memory",
            next_command="kalshi-bot memory-backfill",
        ),
        SourceSpec(
            source_id="phase_3p_self_evaluation",
            source_name="Phase 3P self evaluation",
            source_type="research",
            model=SelfEvaluationJournal,
            attempt_column=SelfEvaluationJournal.created_at,
            success_column=SelfEvaluationJournal.generated_at,
            watermark_column=SelfEvaluationJournal.evaluation_as_of,
            required=False,
            producer_stage="Phase 3P self evaluation",
            next_command="kalshi-bot self-evaluate",
            enabled_setting="phase_3p_self_evaluation_enabled",
            mode_setting="phase_3p_mode",
        ),
        SourceSpec(
            source_id="phase_3q_feature_discovery",
            source_name="Phase 3Q feature discovery",
            source_type="research",
            model=FeatureDiscoveryRun,
            attempt_column=FeatureDiscoveryRun.requested_at,
            success_column=FeatureDiscoveryRun.completed_at,
            watermark_column=FeatureDiscoveryRun.training_as_of,
            required=False,
            producer_stage="Phase 3Q feature discovery",
            next_command="kalshi-bot feature-discovery-run",
            enabled_setting="phase_3q_feature_discovery_enabled",
            mode_setting="phase_3q_mode",
        ),
        SourceSpec(
            source_id="phase_3r_synthetic_markets",
            source_name="Phase 3R synthetic markets",
            source_type="research",
            model=SyntheticMarketRun,
            attempt_column=SyntheticMarketRun.started_at,
            success_column=SyntheticMarketRun.completed_at,
            watermark_column=SyntheticMarketRun.completed_at,
            required=False,
            producer_stage="Phase 3R synthetic markets",
            next_command="kalshi-bot synthetic-markets-run --enable-research",
            enabled_setting="phase_3r_synthetic_markets_enabled",
            mode_setting="phase_3r_mode",
        ),
        SourceSpec(
            source_id="phase_3s_roi_policy",
            source_name="Phase 3S ROI policy",
            source_type="research",
            model=RlRun,
            attempt_column=RlRun.started_at,
            success_column=RlRun.completed_at,
            watermark_column=RlRun.training_as_of,
            required=False,
            producer_stage="Phase 3S ROI policy",
            next_command="kalshi-bot rl-train",
            enabled_setting="phase_3s_reinforcement_learning_enabled",
            mode_setting="phase_3s_mode",
        ),
        SourceSpec(
            source_id="phase_3w_system_certification",
            source_name="Phase 3W system certification",
            source_type="governance",
            model=SystemCertificationRun,
            attempt_column=SystemCertificationRun.started_at,
            success_column=SystemCertificationRun.completed_at,
            watermark_column=SystemCertificationRun.completed_at,
            required=True,
            producer_stage="Phase 3W certification",
            next_command="kalshi-bot system-certification-run",
            enabled_setting="phase_3w_system_certification_enabled",
            mode_setting="phase_3w_mode",
        ),
        SourceSpec(
            source_id="phase_3v_live_readiness",
            source_name="Phase 3V live readiness",
            source_type="governance",
            model=ReadinessDecisionRecord,
            attempt_column=ReadinessDecisionRecord.created_at,
            success_column=ReadinessDecisionRecord.created_at,
            watermark_column=ReadinessDecisionRecord.created_at,
            required=True,
            producer_stage="Phase 3V readiness",
            next_command="kalshi-bot live-readiness-review",
            enabled_setting="phase_3v_live_readiness_enabled",
            mode_setting="phase_3v_mode",
        ),
    )


def _source_enabled(settings: Settings, spec: SourceSpec) -> bool:
    if spec.enabled_setting is not None and not bool(
        getattr(settings, spec.enabled_setting, False)
    ):
        return False
    if spec.mode_setting is not None:
        mode = str(getattr(settings, spec.mode_setting, "") or "").strip().lower()
        if mode in {"", "disabled", "off", "none"}:
            return False
    return True


def _lifecycle_state(
    session: Session,
    *,
    spec: SourceSpec,
    enabled: bool,
    row_count: int,
) -> str:
    if not enabled:
        return LIFECYCLE_DISABLED
    if row_count > 0:
        return LIFECYCLE_ACTIVE
    if spec.source_id == "settlement_state" and _row_count(session, PaperOrder) == 0:
        return LIFECYCLE_WAITING_FOR_OUTCOMES
    if spec.source_id == "trade_pnl_state" and _row_count(session, Settlement) == 0:
        return LIFECYCLE_WAITING_FOR_OUTCOMES
    if spec.source_id == "phase_3q_feature_discovery" and _row_count(session, Forecast) == 0:
        return LIFECYCLE_BLOCKED_BY_DEPENDENCY
    if spec.source_id == "phase_3s_roi_policy" and _row_count(session, PaperPnl) == 0:
        return LIFECYCLE_WAITING_FOR_OUTCOMES
    if spec.source_id == "phase_3w_system_certification" and not _required_chain_has_rows(
        session,
        before_source_id=spec.source_id,
    ):
        return LIFECYCLE_BLOCKED_BY_DEPENDENCY
    if (
        spec.source_id == "phase_3v_live_readiness"
        and _row_count(session, SystemCertificationRun) == 0
    ):
        return LIFECYCLE_BLOCKED_BY_DEPENDENCY
    if spec.source_id == "phase_3p_self_evaluation":
        return LIFECYCLE_UNINITIALIZED
    if spec.source_id == "phase_3r_synthetic_markets":
        return LIFECYCLE_UNINITIALIZED
    return LIFECYCLE_UNINITIALIZED


def _required_chain_has_rows(session: Session, *, before_source_id: str | None = None) -> bool:
    for spec in _source_specs():
        if spec.source_id == before_source_id:
            break
        if spec.required and _row_count(session, spec.model) == 0:
            return False
    return True


def _source_completeness_status(
    *,
    spec: SourceSpec,
    enabled: bool,
    lifecycle: str,
    row_count: int,
) -> str:
    if not enabled and not spec.required:
        return COMPLETENESS_NOT_APPLICABLE
    if lifecycle in {LIFECYCLE_DISABLED, LIFECYCLE_NOT_APPLICABLE} and not spec.required:
        return COMPLETENESS_NOT_APPLICABLE
    if lifecycle == LIFECYCLE_WAITING_FOR_OUTCOMES and not spec.required:
        return COMPLETENESS_NOT_APPLICABLE
    if row_count == 0:
        return COMPLETENESS_UNAVAILABLE if spec.required else COMPLETENESS_PARTIAL
    return COMPLETENESS_COMPLETE


def _source_error(
    session: Session,
    *,
    spec: SourceSpec,
    enabled: bool,
    lifecycle: str,
) -> str | None:
    if lifecycle == LIFECYCLE_DISABLED:
        setting = spec.enabled_setting or spec.mode_setting or "source mode"
        return f"{setting} disables this source."
    if lifecycle == LIFECYCLE_BLOCKED_BY_DEPENDENCY:
        if spec.source_id == "phase_3q_feature_discovery":
            return "Feature discovery is blocked until forecasts exist."
        if spec.source_id == "phase_3w_system_certification":
            missing = [
                item.source_id
                for item in _source_specs()
                if item.required
                and item.source_id != spec.source_id
                and _row_count(session, item.model) == 0
            ]
            return f"Certification is blocked by missing required sources: {', '.join(missing)}."
        if spec.source_id == "phase_3v_live_readiness":
            return "Live readiness is blocked until Phase 3W certification evidence exists."
        return "Source is blocked by an upstream dependency."
    if lifecycle == LIFECYCLE_WAITING_FOR_OUTCOMES:
        return "Source is waiting for settled paper outcomes."
    if not enabled:
        return "Source is disabled."
    return None


def _source_warning(
    *,
    spec: SourceSpec,
    freshness: str,
    completeness: str,
    lifecycle: str,
    error: str | None,
) -> str | None:
    if lifecycle in {LIFECYCLE_DISABLED, LIFECYCLE_NOT_APPLICABLE} and not spec.required:
        return None
    if completeness == COMPLETENESS_UNAVAILABLE:
        return f"{spec.source_id} has no rows."
    if freshness == FRESHNESS_STALE:
        return f"{spec.source_id} is stale."
    if spec.required and lifecycle in {
        LIFECYCLE_BLOCKED_BY_DEPENDENCY,
        LIFECYCLE_DISABLED,
        LIFECYCLE_WAITING_FOR_OUTCOMES,
    }:
        return error or f"{spec.source_id} is {lifecycle.lower()}."
    return None


def _row_count(session: Session, model: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _runtime_context(
    session: Session,
    *,
    settings: Settings,
    config: DashboardConfig,
) -> dict[str, Any]:
    database_url = redact_database_url(database_url_from_settings(settings))
    return {
        "environment": settings.kalshi_env.upper(),
        "execution_mode": _execution_mode(settings),
        "timezone": config.display_timezone,
        "database_url": database_url,
        "database_fingerprint": _database_fingerprint(session, settings=settings),
        "git_commit": _git_commit(),
        "repository_root": str(Path.cwd()),
        "phase_3t_mode": config.mode.upper(),
    }


def _database_fingerprint(session: Session, *, settings: Settings) -> str:
    try:
        raw_url = str(session.get_bind().url)
    except Exception:
        raw_url = database_url_from_settings(settings)
    redacted = redact_database_url(raw_url)
    return query_hash({"database_url": redacted})


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return "unknown"
    commit = result.stdout.strip()
    return commit or "unknown"


def _cross_panel_skew(
    watermarks: list[SourceWatermark],
    *,
    config: DashboardConfig,
) -> dict[str, Any]:
    dated = [
        (item, parse_datetime(item.data_watermark or item.latest_at))
        for item in watermarks
        if item.enabled
        and item.completeness_status == COMPLETENESS_COMPLETE
        and (item.data_watermark or item.latest_at)
    ]
    present = [(item, value) for item, value in dated if value is not None]
    if len(present) < 2:
        return {
            "status": "UNKNOWN",
            "consistency_status": "WATERMARK_UNKNOWN",
            "value_seconds": None,
            "unit": "seconds",
            "threshold_seconds": config.max_source_skew_seconds,
            "oldest_source_id": None,
            "oldest_watermark": None,
            "newest_source_id": None,
            "newest_watermark": None,
            "source_pair": None,
        }
    oldest = min(present, key=lambda item: item[1])
    newest = max(present, key=lambda item: item[1])
    seconds = int(max(0, (newest[1] - oldest[1]).total_seconds()))
    degraded = seconds > config.max_source_skew_seconds
    return {
        "status": "DEGRADED" if degraded else "OK",
        "consistency_status": "CONSISTENCY_DEGRADED" if degraded else "WATERMARK_ALIGNED",
        "value_seconds": seconds,
        "unit": "seconds",
        "threshold_seconds": config.max_source_skew_seconds,
        "oldest_source_id": oldest[0].source_id,
        "oldest_watermark": oldest[1].isoformat(),
        "newest_source_id": newest[0].source_id,
        "newest_watermark": newest[1].isoformat(),
        "source_pair": {
            "oldest_source_id": oldest[0].source_id,
            "newest_source_id": newest[0].source_id,
        },
    }


def _phase_3t_gate(
    *,
    config: DashboardConfig,
    watermarks: list[SourceWatermark],
    freshness_status: str,
    completeness_status: str,
    skew: dict[str, Any],
) -> dict[str, Any]:
    blockers = []
    if not config.enabled or config.mode == MODE_DISABLED:
        blockers.append("Phase 3T dashboard mode is disabled.")
    if freshness_status in {FRESHNESS_STALE, FRESHNESS_UNKNOWN}:
        blockers.append("Required source freshness is stale or unknown.")
    if completeness_status == COMPLETENESS_UNAVAILABLE:
        blockers.append("Required source completeness is unavailable.")
    if skew["status"] == "DEGRADED":
        blockers.append("Cross-panel source watermarks exceed the skew threshold.")
    for item in watermarks:
        if item.required and item.lifecycle_state in {
            LIFECYCLE_BLOCKED_BY_DEPENDENCY,
            LIFECYCLE_DISABLED,
        }:
            blockers.append(f"{item.source_id}: {item.error or item.lifecycle_state}.")
    status = "PASS" if not blockers else "BLOCKED"
    return {
        "status": status,
        "can_generate_fresh_snapshot": status == "PASS",
        "required_source_count": sum(1 for item in watermarks if item.required),
        "required_sources_ready": [
            item.source_id
            for item in watermarks
            if item.required
            and item.freshness_status in {FRESHNESS_FRESH, FRESHNESS_AGING}
            and item.completeness_status == COMPLETENESS_COMPLETE
        ],
        "blockers": blockers,
    }


def _producer_chain(watermarks: list[SourceWatermark]) -> list[dict[str, Any]]:
    return [
        {
            "stage": item.producer_stage,
            "source_id": item.source_id,
            "required": item.required,
            "enabled": item.enabled,
            "lifecycle_state": item.lifecycle_state,
            "freshness_status": item.freshness_status,
            "completeness_status": item.completeness_status,
            "data_watermark": item.data_watermark,
            "next_command": item.next_command,
            "error": item.error,
        }
        for item in watermarks
    ]


def _market_heatmap(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rankings = list(
        session.scalars(
            select(MarketRanking)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
            .limit(limit * 3)
        )
    )
    groups: dict[tuple[str, str], list[MarketRanking]] = defaultdict(list)
    for ranking in rankings:
        market = session.get(Market, ranking.ticker)
        category = classify_market_category(ranking.title or (market.title if market else ""))
        groups[(category, ranking.forecast_model)].append(ranking)
    rows = []
    for (category, model), items in sorted(groups.items()):
        rows.append(
            {
                "category": category,
                "model": model,
                "markets": len(items),
                "avg_opportunity_score": decimal_or_na(
                    _average(row.opportunity_score for row in items)
                ),
                "avg_spread": decimal_or_na(_average(row.spread for row in items)),
                "avg_liquidity": decimal_or_na(_average(row.liquidity_score for row in items)),
                "as_of": max(row.ranked_at for row in items).isoformat(),
                "source": "market_rankings",
            }
        )
    return rows[:limit]


def _opportunity_rows(
    session: Session,
    *,
    limit: int,
    filters: dict[str, Any],
    settings: Settings,
) -> list[dict[str, Any]]:
    model_filter = _filter_value(filters, "model_id")
    category_filter = _filter_value(filters, "category")
    statement = select(MarketRanking).order_by(
        desc(MarketRanking.opportunity_score),
        desc(MarketRanking.ranked_at),
        desc(MarketRanking.id),
    )
    if model_filter:
        statement = statement.where(MarketRanking.forecast_model == model_filter)
    rows = []
    seen: set[str] = set()
    for ranking in session.scalars(statement.limit(limit * 4)):
        if ranking.ticker in seen:
            continue
        seen.add(ranking.ticker)
        market = session.get(Market, ranking.ticker)
        identity = verify_market_identity(
            session,
            ranking=ranking,
            market=market,
            settings=settings,
        )
        identity_fields = market_identity_fields(identity)
        category = identity.category or classify_market_category(
            ranking.title or (market.title if market else "")
        )
        if category_filter and category_filter.lower() != category.lower():
            continue
        risk = _latest_risk_for_ticker(session, ranking.ticker)
        rows.append(
            {
                "ticker": ranking.ticker,
                **identity_fields,
                "market_identity": identity.as_dict(),
                "title": identity.market_title
                or ranking.title
                or (market.title if market else ranking.ticker),
                "category": category,
                "model": ranking.forecast_model,
                "market_probability": ranking.midpoint or ranking.best_price or "n/a",
                "model_probability": ranking.forecast_probability or "n/a",
                "edge": ranking.estimated_edge or "n/a",
                "opportunity_score": ranking.opportunity_score or "n/a",
                "spread": ranking.spread or "n/a",
                "liquidity": ranking.liquidity_score or "n/a",
                "phase_3n_action": risk.action if risk else "UNKNOWN",
                "phase_3n_reason": _reason_codes(risk.reason_codes_json if risk else None),
                "source": "market_rankings",
                "as_of": ranking.ranked_at.isoformat(),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _model_matrix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        sample = int(row.get("forecast_count") or 0)
        output.append(
            {
                "model_name": row.get("model_name"),
                "forecast_count": sample,
                "win_rate": row.get("win_rate", "n/a") if sample else "n/a",
                "roi": row.get("roi", "n/a") if sample else "n/a",
                "status": "INSUFFICIENT_SAMPLE" if sample < 5 else row.get("rank_label", "READY"),
                "sample_warning": sample < 5,
                "source": "model_leaderboard",
            }
        )
    return output


def _risk_decisions(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(AdvancedRiskDecisionLog)
        .order_by(
            desc(AdvancedRiskDecisionLog.decision_timestamp),
            desc(AdvancedRiskDecisionLog.id),
        )
        .limit(limit)
    )
    return [
        {
            "decision_timestamp": row.decision_timestamp.isoformat(),
            "ticker": row.ticker,
            "action": row.action,
            "phase_3m_proposed_contracts": row.phase_3m_proposed_contracts,
            "live_candidate_contracts": row.live_candidate_contracts,
            "executed_contracts": row.executed_contracts,
            "reason_codes": _reason_codes(row.reason_codes_json),
            "block_preserved": row.action.upper() == "BLOCK" and row.executed_contracts == 0,
        }
        for row in rows
    ]


def _trade_rows(session: Session, *, limit: int) -> list[dict[str, Any]]:
    orders = list(
        session.scalars(
            select(PaperOrder)
            .order_by(desc(PaperOrder.created_at), desc(PaperOrder.id))
            .limit(limit)
        )
    )
    fills_by_order = Counter(
        row.paper_order_id
        for row in session.scalars(
            select(PaperFill)
            .order_by(desc(PaperFill.filled_at), desc(PaperFill.id))
            .limit(limit * 4)
        )
    )
    rows = []
    for order in orders:
        rows.append(
            {
                "trade_id": f"paper_order:{order.id}",
                "ticker": order.ticker,
                "created_at": order.created_at.isoformat(),
                "model": order.model_name,
                "execution_mode": "PAPER",
                "side": order.side,
                "quantity": order.quantity,
                "status": order.status,
                "fills": fills_by_order.get(order.id, 0),
                "evidence_type": "PAPER_SIMULATED",
                "source": "paper_orders",
            }
        )
    if rows:
        return rows
    memory_rows = session.scalars(
        select(TradeMemory)
        .order_by(desc(TradeMemory.event_time), desc(TradeMemory.trade_memory_event_id))
        .limit(limit)
    )
    return [
        {
            "trade_id": row.trade_id,
            "ticker": row.instrument_id,
            "created_at": row.event_time.isoformat(),
            "model": row.model_id or "n/a",
            "execution_mode": row.execution_mode,
            "side": row.direction,
            "quantity": row.filled_quantity,
            "status": row.settlement_status or "UNKNOWN",
            "fills": row.filled_quantity or 0,
            "evidence_type": "LIVE_REALIZED" if row.execution_mode == "LIVE" else "PAPER_SIMULATED",
            "source": "trade_memory",
        }
        for row in memory_rows
    ]


def _research_layers(session: Session) -> dict[str, Any]:
    journal = session.scalar(
        select(SelfEvaluationJournal)
        .order_by(desc(SelfEvaluationJournal.created_at), desc(SelfEvaluationJournal.journal_id))
        .limit(1)
    )
    return {
        "phase_3p": {
            "latest_journal_id": journal.journal_id if journal else None,
            "status": journal.journal_status if journal else "NOT_RUN",
            "latest_at": journal.created_at.isoformat() if journal else None,
        },
        "phase_3q": feature_discovery_status(session),
        "phase_3r": {
            **synthetic_markets_status(session),
            "synthetic_label": "SYNTHETIC INTERNAL NON_TRADABLE",
            "order_actions_allowed": False,
        },
        "phase_3s": {
            **rl_status(session),
            "phase_3s_action_is_order": False,
            "fallback_visible": True,
        },
    }


def _reconciliation(session: Session, panels: dict[str, Any]) -> dict[str, Any]:
    panel_position_count = len(panels["exposure_maps"]["positions"])
    authoritative_position_count = int(
        session.scalar(select(func.count()).select_from(PaperPosition)) or 0
    )
    panel_order_count = len(panels["trade_blotter"])
    authoritative_order_count = int(
        session.scalar(select(func.count()).select_from(PaperOrder)) or 0
    )
    checks = [
        {
            "check_id": "position_count",
            "status": "PASS" if panel_position_count <= authoritative_position_count else "FAIL",
            "panel_value": panel_position_count,
            "authoritative_value": authoritative_position_count,
            "source": "paper_positions",
        },
        {
            "check_id": "paper_order_count",
            "status": (
                "PASS"
                if panel_order_count <= max(authoritative_order_count, panel_order_count)
                else "FAIL"
            ),
            "panel_value": panel_order_count,
            "authoritative_value": authoritative_order_count,
            "source": "paper_orders",
        },
    ]
    return {
        "status": "PASS" if all(check["status"] == "PASS" for check in checks) else "FAIL",
        "checks": checks,
    }


def _panel_registry(config: DashboardConfig) -> list[dict[str, Any]]:
    return [
        {
            **definition,
            "panel_definition_version": config.panel_registry_version,
            "source_units_visible": True,
            "read_only": True,
        }
        for definition in PANEL_DEFINITIONS
    ]


def _execution_mode(settings: Settings) -> str:
    if settings.kalshi_env.lower() in {"live", "production"}:
        return "LIVE_READ_ONLY"
    if settings.execution_enabled:
        return "DEMO_GATED"
    return "PAPER_SHADOW"


def _time_mode(config: DashboardConfig) -> str:
    if config.mode == "historical_replay":
        return "HISTORICAL_REPLAY"
    if config.mode == MODE_DISABLED:
        return "DISABLED"
    return "LIVE_UPDATING"


def _effective_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    defaults = {
        "time_mode": "LIVE_UPDATING",
        "execution_mode": "paper_shadow",
        "category": "ALL",
        "model_id": "ALL",
        "phase_3n_decision": "ALL",
        "settlement_finality": "ALL",
    }
    if not filters:
        return defaults
    return {**defaults, **{key: value for key, value in filters.items() if value not in (None, "")}}


def _filter_value(filters: dict[str, Any], key: str) -> str | None:
    value = filters.get(key)
    if value in (None, "", "ALL", []):
        return None
    return str(value)


def _latest_risk_for_ticker(session: Session, ticker: str) -> AdvancedRiskDecisionLog | None:
    return session.scalar(
        select(AdvancedRiskDecisionLog)
        .where(AdvancedRiskDecisionLog.ticker == ticker)
        .order_by(
            desc(AdvancedRiskDecisionLog.decision_timestamp),
            desc(AdvancedRiskDecisionLog.id),
        )
        .limit(1)
    )


def _reason_codes(value: str | None) -> list[str]:
    payload = decode_json(value)
    if isinstance(payload, dict):
        raw = payload.get("reason_codes") or payload.get("reasons") or []
        return [str(item) for item in raw]
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return []


def _average(values: Any) -> Decimal | None:
    decimals = [to_decimal(value) for value in values]
    present = [value for value in decimals if value is not None]
    if not present:
        return None
    return sum(present, Decimal("0")) / Decimal(len(present))


def _warning_count(watermarks: list[SourceWatermark]) -> int:
    return sum(1 for item in watermarks if item.warning)


def _known_or_na(value: Any) -> str:
    return decimal_or_na(to_decimal(value))


def _watermark_signature(watermarks: list[SourceWatermark]) -> str:
    return canonical_json(
        {
            item.source_id: {
                "latest_at": item.latest_at,
                "row_count": item.row_count,
                "freshness": item.freshness_status,
            }
            for item in watermarks
        }
    )
