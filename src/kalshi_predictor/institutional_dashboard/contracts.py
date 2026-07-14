from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import decimal_to_str

SNAPSHOT_SCHEMA_VERSION = "phase-3t-dashboard-snapshot-v1"
API_SCHEMA_VERSION = "phase-3t-dashboard-api-v1"
PANEL_REGISTRY_VERSION = "phase_3t_panel_registry_v1"
METRIC_CATALOG_VERSION = "phase_3t_metric_catalog_v1"

MODE_DISABLED = "disabled"
MODE_HISTORICAL_REPLAY = "historical_replay"
MODE_READ_ONLY_SHADOW = "read_only_shadow"
MODE_READ_ONLY_LIVE = "read_only_live"
MODES = {
    MODE_DISABLED,
    MODE_HISTORICAL_REPLAY,
    MODE_READ_ONLY_SHADOW,
    MODE_READ_ONLY_LIVE,
}

FRESHNESS_FRESH = "FRESH"
FRESHNESS_AGING = "AGING"
FRESHNESS_STALE = "STALE"
FRESHNESS_UNKNOWN = "UNKNOWN"
FRESHNESS_NOT_APPLICABLE = "NOT_APPLICABLE"
COMPLETENESS_COMPLETE = "COMPLETE"
COMPLETENESS_PARTIAL = "PARTIAL"
COMPLETENESS_UNAVAILABLE = "UNAVAILABLE"
COMPLETENESS_NOT_APPLICABLE = "NOT_APPLICABLE"

LIFECYCLE_ACTIVE = "ACTIVE"
LIFECYCLE_BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"
LIFECYCLE_DISABLED = "DISABLED"
LIFECYCLE_NOT_APPLICABLE = "NOT_APPLICABLE"
LIFECYCLE_UNINITIALIZED = "UNINITIALIZED"
LIFECYCLE_WAITING_FOR_OUTCOMES = "WAITING_FOR_OUTCOMES"

PANEL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "panel_id": "environment_status",
        "panel_type": "status_header",
        "title": "Environment And System Status",
        "criticality": "critical",
        "sources": ["settings", "source_watermarks"],
        "metrics": ["dashboard_mode", "snapshot_age", "feed_health"],
        "honored_filters": ["as_of", "time_mode", "execution_mode"],
        "freshness_policy": "required_sources_cannot_be_stale",
        "drilldown": "/institutional",
    },
    {
        "panel_id": "kpi_ribbon",
        "panel_type": "kpi",
        "title": "KPI Ribbon",
        "criticality": "critical",
        "sources": ["portfolio_summary", "paper_ledger", "advanced_risk"],
        "metrics": ["net_pnl", "risk_used", "open_positions", "alerts"],
        "honored_filters": ["execution_mode", "account_scope"],
        "freshness_policy": "paper_and_risk_watermarks",
        "drilldown": "/portfolio",
    },
    {
        "panel_id": "market_heatmap",
        "panel_type": "heatmap",
        "title": "Market Heat Map",
        "criticality": "high",
        "sources": ["market_rankings", "market_snapshots"],
        "metrics": ["opportunity_score", "spread", "liquidity", "confidence"],
        "honored_filters": ["category", "model_id", "liquidity_tier", "spread_tier"],
        "freshness_policy": "market_data_threshold",
        "drilldown": "/markets",
    },
    {
        "panel_id": "opportunity_scanner",
        "panel_type": "scanner",
        "title": "Opportunity Scanner",
        "criticality": "critical",
        "sources": ["market_rankings", "forecasts", "advanced_risk"],
        "metrics": ["edge", "opportunity_score", "phase_3m_size", "phase_3n_action"],
        "honored_filters": ["category", "model_id", "phase_3n_decision"],
        "freshness_policy": "opportunities_and_risk",
        "drilldown": "/opportunities",
    },
    {
        "panel_id": "personal_trader_brief",
        "panel_type": "advisory",
        "title": "Phase 3U Personal AI Trader Brief",
        "criticality": "critical",
        "sources": ["personal_trader", "market_rankings", "advanced_risk"],
        "metrics": ["recommended_count", "no_trade", "risk_adjusted_ev"],
        "honored_filters": ["category", "model_id", "execution_mode"],
        "freshness_policy": "phase_3u_advisory_snapshot",
        "drilldown": "/personal-trader",
    },
    {
        "panel_id": "model_matrix",
        "panel_type": "matrix",
        "title": "Model Matrix",
        "criticality": "high",
        "sources": ["model_leaderboard", "model_status"],
        "metrics": ["forecast_count", "win_rate", "roi", "confidence"],
        "honored_filters": ["model_id", "model_version"],
        "freshness_policy": "model_performance_watermark",
        "drilldown": "/models",
    },
    {
        "panel_id": "exposure_maps",
        "panel_type": "exposure",
        "title": "Exposure Maps",
        "criticality": "critical",
        "sources": ["paper_positions", "portfolio_summary"],
        "metrics": ["category_exposure", "market_exposure", "direction_exposure"],
        "honored_filters": ["category", "event", "market", "direction"],
        "freshness_policy": "position_update_watermark",
        "drilldown": "/portfolio",
    },
    {
        "panel_id": "risk_waterfall",
        "panel_type": "risk",
        "title": "Risk Limits And Decision Waterfall",
        "criticality": "critical",
        "sources": ["position_sizing_decisions", "advanced_risk_decisions"],
        "metrics": ["phase_3m_proposed", "phase_3n_action", "blocked_count"],
        "honored_filters": ["phase_3m_tier", "phase_3n_decision", "reason_code"],
        "freshness_policy": "risk_decision_watermark",
        "drilldown": "/reports/advanced_risk_report.md",
    },
    {
        "panel_id": "live_readiness",
        "panel_type": "governance",
        "title": "Phase 3V Live Readiness Review",
        "criticality": "critical",
        "sources": ["readiness_decisions", "readiness_controls"],
        "metrics": ["decision", "critical_blockers", "certificate"],
        "honored_filters": ["execution_mode", "account_scope"],
        "freshness_policy": "readiness_review_watermark",
        "drilldown": "/live-readiness",
    },
    {
        "panel_id": "system_certification",
        "panel_type": "governance",
        "title": "Phase 3W End-to-End Certification",
        "criticality": "critical",
        "sources": ["system_certification_runs", "readiness_decisions"],
        "metrics": ["overall_status", "phase_count", "connection_count"],
        "honored_filters": ["execution_mode", "account_scope"],
        "freshness_policy": "certification_run_watermark",
        "drilldown": "/system-certification",
    },
    {
        "panel_id": "trade_blotter",
        "panel_type": "table",
        "title": "Trade, Fill, Settlement, And Outcome Blotter",
        "criticality": "high",
        "sources": ["paper_orders", "paper_fills", "trade_memory"],
        "metrics": ["order_status", "fill_quality", "settlement_status", "net_pnl"],
        "honored_filters": ["trade_state", "settlement_finality", "execution_mode"],
        "freshness_policy": "trade_memory_watermark",
        "drilldown": "/portfolio",
    },
    {
        "panel_id": "system_health",
        "panel_type": "health",
        "title": "System Health And Data Freshness",
        "criticality": "critical",
        "sources": ["database", "market_memory", "source_watermarks"],
        "metrics": ["freshness_status", "completeness_status", "source_lag"],
        "honored_filters": ["data_quality"],
        "freshness_policy": "all_required_sources",
        "drilldown": "/settings/database",
    },
    {
        "panel_id": "research_layers",
        "panel_type": "research",
        "title": "Phase 3P-3S Research And Learning",
        "criticality": "medium",
        "sources": ["self_evaluation", "feature_discovery", "synthetic_markets", "rl_policy"],
        "metrics": ["latest_status", "sample_size", "support_status", "non_tradable_flag"],
        "honored_filters": ["model_id", "phase_3s_action"],
        "freshness_policy": "best_effort_research",
        "drilldown": "/learning",
    },
)

METRIC_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "metric_id": "net_pnl",
        "unit": "currency",
        "authority": "paper.pnl / portfolio_summary",
        "unknown_policy": "UNKNOWN is rendered as n/a, never zero.",
    },
    {
        "metric_id": "risk_used",
        "unit": "contracts_or_currency_by_source",
        "authority": "Phase 3N advanced_risk_decisions and reservations",
        "unknown_policy": "Unavailable limits remain unavailable.",
    },
    {
        "metric_id": "opportunity_score",
        "unit": "score_0_100",
        "authority": "market_rankings",
        "unknown_policy": "Missing score sorts behind known scores.",
    },
    {
        "metric_id": "roi",
        "unit": "ratio",
        "authority": "leaderboard, paper P&L, Phase 3S reward definition",
        "unknown_policy": "ROI is shown with sample support and evidence type.",
    },
)


@dataclass(frozen=True)
class DashboardConfig:
    enabled: bool
    mode: str
    dashboard_definition_version: str
    panel_registry_version: str
    metric_catalog_version: str
    snapshot_validity_seconds: int
    fresh_after_seconds: int
    stale_after_seconds: int
    max_source_skew_seconds: int
    max_rows_per_panel: int
    display_timezone: str = "America/Chicago"

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"Unsupported Phase 3T mode: {self.mode}")
        if self.enabled and self.mode == MODE_DISABLED:
            raise ValueError("Enabled Phase 3T dashboard requires a read-only mode.")
        if self.max_rows_per_panel <= 0:
            raise ValueError("Phase 3T max rows per panel must be positive.")


@dataclass(frozen=True)
class SourceWatermark:
    source_id: str
    source_name: str
    source_type: str
    required: bool
    requirement_state: str
    enabled: bool
    lifecycle_state: str
    last_attempt_at: str | None
    last_success_at: str | None
    data_watermark: str | None
    latest_at: str | None
    row_count: int
    freshness_threshold_seconds: int
    freshness_status: str
    completeness_status: str
    error: str | None
    warning: str | None
    database_fingerprint: str
    git_commit: str
    producer_stage: str
    next_command: str | None

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def stable_id(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kalshi_predictor:phase_3t:{text}"))


def canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), default=str)


def query_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


def decimal_or_na(value: Any) -> str:
    return decimal_to_str(value) or "n/a"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_to_str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
