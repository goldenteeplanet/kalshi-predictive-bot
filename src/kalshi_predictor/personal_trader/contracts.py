from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings
from kalshi_predictor.utils.decimals import decimal_to_str

BRIEF_SCHEMA_VERSION = "1.0.0"
CARD_SCHEMA_VERSION = "1.0.0"
QUERY_SCHEMA_VERSION = "phase-3u-query-v1"
API_SCHEMA_VERSION = "phase-3u-api-v1"

MODE_DISABLED = "DISABLED"
MODE_OFFLINE_REPLAY = "OFFLINE_REPLAY"
MODE_SHADOW = "SHADOW"
MODE_PAPER_ADVISORY = "PAPER_ADVISORY"
MODE_LIVE_ADVISORY = "LIVE_ADVISORY"
MODES = {
    MODE_DISABLED,
    MODE_OFFLINE_REPLAY,
    MODE_SHADOW,
    MODE_PAPER_ADVISORY,
    MODE_LIVE_ADVISORY,
}

STATUS_ACTIONABLE = "ACTIONABLE_ADVISORY"
STATUS_WATCHLIST = "WATCHLIST_ONLY"
STATUS_REJECTED = "REJECTED"
STATUS_SYNTHETIC = "SYNTHETIC_RESEARCH"

EVENT_BRIEF_REQUESTED = "BRIEF_REQUESTED"
EVENT_QUERY_NORMALIZED = "QUERY_NORMALIZED"
EVENT_SNAPSHOT_CAPTURED = "SNAPSHOT_CAPTURED"
EVENT_CANDIDATE_EVALUATED = "CANDIDATE_EVALUATED"
EVENT_CANDIDATE_REJECTED = "CANDIDATE_REJECTED"
EVENT_CANDIDATE_RANKED = "CANDIDATE_RANKED"
EVENT_BRIEF_ISSUED = "BRIEF_ISSUED"

ELIGIBILITY_POLICY_VERSION = "3u-eligibility-v1"
RANKING_POLICY_VERSION = "3u-rank-v1"
EXPLANATION_POLICY_VERSION = "3u-explain-v1"
METRIC_CATALOG_VERSION = "3u-metrics-v1"

READ_ONLY_BOUNDARY = {
    "advisory_only": True,
    "allow_exchange_writes": False,
    "allow_order_create": False,
    "allow_order_cancel": False,
    "allow_order_replace": False,
    "allow_risk_override": False,
    "allow_size_increase": False,
    "allow_live_execution": False,
    "allow_demo_execution": False,
    "llm_may_change_rank": False,
    "llm_may_change_side": False,
    "llm_may_change_quantity": False,
    "llm_may_change_ev": False,
    "synthetic_markets_actionable": False,
}

REJECTION_CATEGORIES = {
    "PHASE_3U_DISABLED": "other",
    "MARKET_NOT_OPEN": "market_quality",
    "MARKET_CLOSE_MISSING": "market_quality",
    "MARKET_ALREADY_CLOSED": "market_quality",
    "SETTLEMENT_TERMS_MISSING": "market_quality",
    "QUOTE_MISSING": "market_quality",
    "QUOTE_STALE": "market_quality",
    "FORECAST_MISSING": "model_quality",
    "FORECAST_STALE": "model_quality",
    "OPPORTUNITY_STALE": "market_quality",
    "SPREAD_LIMIT_EXCEEDED": "market_quality",
    "MISSING_EDGE": "economics",
    "NET_EV_BELOW_MINIMUM": "economics",
    "ROI_BELOW_MINIMUM": "economics",
    "RISK_ADJUSTED_EV_BELOW_MINIMUM": "economics",
    "PHASE_3S_SKIP": "risk",
    "PHASE_3S_OOD": "risk",
    "PHASE_3S_UNSUPPORTED": "risk",
    "PHASE_3M_SIZE_MISSING": "risk",
    "PHASE_3M_ZERO_SIZE": "risk",
    "PHASE_3N_DECISION_MISSING": "risk",
    "PHASE_3N_BLOCK": "risk",
    "PHASE_3N_ZERO_QUANTITY": "risk",
    "PHASE_3N_STALE": "risk",
    "USER_FILTERED": "user_filtered",
    "REDUNDANT_WITH_HIGHER_RANK": "risk",
}


@dataclass(frozen=True)
class PersonalTraderConfig:
    enabled: bool
    mode: str
    schema_version: str
    ranking_policy_version: str
    eligibility_policy_version: str
    explanation_policy_version: str
    default_timezone: str
    default_maximum_recommendations: int
    absolute_maximum_recommendations: int
    min_net_ev_per_contract: Decimal
    min_expected_roi: Decimal
    min_risk_adjusted_ev_lcb_per_contract: Decimal
    max_spread: Decimal
    max_quote_age_seconds: int
    max_forecast_age_seconds: int
    max_opportunity_age_seconds: int
    max_risk_age_seconds: int
    max_advisory_lifetime_seconds: int
    candidate_limit: int
    allow_phase_3s_fallback: bool
    llm_renderer_enabled: bool

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"Unsupported Phase 3U mode: {self.mode}")
        if self.enabled and self.mode == MODE_DISABLED:
            raise ValueError("Enabled Phase 3U requires SHADOW, PAPER_ADVISORY, or replay mode.")
        if self.default_maximum_recommendations < 1:
            raise ValueError("Phase 3U default maximum recommendations must be positive.")
        if self.absolute_maximum_recommendations < self.default_maximum_recommendations:
            raise ValueError("Phase 3U absolute maximum recommendations must cover the default.")


@dataclass(frozen=True)
class PersonalTraderQuery:
    query_id: str
    requested_at: datetime
    principal_id: str
    account_scope: str
    portfolio_scope: str
    timezone: str
    natural_language_query: str
    normalized_intent: str
    requested_as_of: datetime
    resolved_day_start: datetime
    resolved_day_end: datetime
    relative_time_expression: str | None
    execution_mode: str
    maximum_recommendations: int
    category_include: tuple[str, ...]
    category_exclude: tuple[str, ...]
    market_include: tuple[str, ...]
    market_exclude: tuple[str, ...]
    risk_preference_override: str
    include_watchlist: bool
    include_synthetic_research: bool
    response_detail_level: str
    locale: str
    profile_version: str
    request_schema_version: str = QUERY_SCHEMA_VERSION

    def effective_filters(self) -> dict[str, Any]:
        return {
            "maximum_recommendations": self.maximum_recommendations,
            "category_include": list(self.category_include),
            "category_exclude": list(self.category_exclude),
            "market_include": list(self.market_include),
            "market_exclude": list(self.market_exclude),
            "risk_preference_override": self.risk_preference_override,
            "include_watchlist": self.include_watchlist,
            "include_synthetic_research": self.include_synthetic_research,
            "response_detail_level": self.response_detail_level,
        }


def config_from_settings(settings: Settings) -> PersonalTraderConfig:
    mode = settings.phase_3u_mode.upper()
    if not settings.phase_3u_personal_ai_trader_enabled:
        mode = MODE_DISABLED
    config = PersonalTraderConfig(
        enabled=settings.phase_3u_personal_ai_trader_enabled,
        mode=mode,
        schema_version=settings.phase_3u_schema_version,
        ranking_policy_version=settings.phase_3u_ranking_policy_version,
        eligibility_policy_version=settings.phase_3u_eligibility_policy_version,
        explanation_policy_version=settings.phase_3u_explanation_policy_version,
        default_timezone=settings.phase_3u_default_timezone,
        default_maximum_recommendations=settings.phase_3u_default_max_recommendations,
        absolute_maximum_recommendations=settings.phase_3u_absolute_max_recommendations,
        min_net_ev_per_contract=settings.phase_3u_min_net_ev_per_contract,
        min_expected_roi=settings.phase_3u_min_expected_roi,
        min_risk_adjusted_ev_lcb_per_contract=(
            settings.phase_3u_min_risk_adjusted_ev_lcb_per_contract
        ),
        max_spread=settings.phase_3u_max_spread,
        max_quote_age_seconds=settings.phase_3u_max_quote_age_seconds,
        max_forecast_age_seconds=settings.phase_3u_max_forecast_age_seconds,
        max_opportunity_age_seconds=settings.phase_3u_max_opportunity_age_seconds,
        max_risk_age_seconds=settings.phase_3u_max_risk_age_seconds,
        max_advisory_lifetime_seconds=settings.phase_3u_max_advisory_lifetime_seconds,
        candidate_limit=settings.phase_3u_candidate_limit,
        allow_phase_3s_fallback=settings.phase_3u_allow_phase_3s_fallback,
        llm_renderer_enabled=settings.phase_3u_llm_renderer_enabled,
    )
    config.validate()
    return config


def stable_id(*parts: Any, prefix: str = "3u") -> str:
    text = "|".join(str(part) for part in parts)
    return f"{prefix}-{uuid.uuid5(uuid.NAMESPACE_URL, f'kalshi_predictor:phase_3u:{text}')}"


def event_id(*parts: Any) -> str:
    return stable_id(*parts, prefix="event")


def canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


def scope_hash(value: str) -> str:
    return stable_hash(value)[:24]


def decimal_string(value: Any, fallback: str = "0") -> str:
    return decimal_to_str(value) or fallback


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_to_str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value
