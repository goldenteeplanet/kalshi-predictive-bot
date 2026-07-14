from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import decimal_to_str

SCHEMA_VERSION = "phase-3r.synthetic-market-card.v1"
CONFIGURATION_VERSION = "phase_3r_default_v1"
GENERATION_POLICY_VERSION = "phase_3r_generation_v1"
LISTING_POLICY_VERSION = "phase_3r_listing_v1"
MODEL_ROUTING_VERSION = "phase_3r_model_routing_v1"
CONSTRAINT_POLICY_VERSION = "phase_3r_constraints_v1"
MODEL_BUNDLE_ID = "synthetic_market_baseline_bundle_v1"
SYNTHETIC_SOURCE_COMPONENT = "phase_3r_synthetic_markets"
DISCLAIMER = "INTERNAL SYNTHETIC FORECAST — NOT A LISTED OR TRADABLE KALSHI MARKET"

MODE_DISABLED = "disabled"
MODE_OFFLINE_REPLAY = "offline_replay"
MODE_SHADOW = "shadow"
MODE_GOVERNED_RESEARCH = "governed_research"
OPERATING_MODES = {MODE_DISABLED, MODE_OFFLINE_REPLAY, MODE_SHADOW, MODE_GOVERNED_RESEARCH}

RUN_CANDIDATE_DISCOVERY = "CANDIDATE_DISCOVERY"
RUN_ESTIMATE_REFRESH = "ESTIMATE_REFRESH"
RUN_LISTING_RECONCILIATION = "LISTING_RECONCILIATION"
RUN_RESOLUTION_PROCESSING = "RESOLUTION_PROCESSING"
RUN_CALIBRATION_ROLLUP = "CALIBRATION_ROLLUP"
RUN_HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
RUN_TYPES = {
    RUN_CANDIDATE_DISCOVERY,
    RUN_ESTIMATE_REFRESH,
    RUN_LISTING_RECONCILIATION,
    RUN_RESOLUTION_PROCESSING,
    RUN_CALIBRATION_ROLLUP,
    RUN_HISTORICAL_REPLAY,
}

LISTING_NO_EXACT_MATCH = "NO_EXACT_MATCH_OBSERVED"
LISTING_UNKNOWN = "LISTING_STATUS_UNKNOWN"
LISTING_EXACT_MATCH = "EXACT_EQUIVALENT_LISTED"
LISTING_RELATED = "RELATED_NOT_COMPARABLE"

EVENT_ACTIVE = "ACTIVE_RESEARCH"
EVENT_REJECTED_POLICY = "REJECTED_POLICY"
EVENT_REJECTED_UNRESOLVABLE = "REJECTED_UNRESOLVABLE"
EVENT_REJECTED_DUPLICATE = "REJECTED_DUPLICATE"
EVENT_REJECTED_INSUFFICIENT_DATA = "REJECTED_INSUFFICIENT_DATA"
EVENT_LISTING_UNKNOWN = "LISTING_STATUS_UNKNOWN"

ESTIMATE_PUBLISHED = "PUBLISHED_INTERNAL"
ESTIMATE_SUPPRESSED = "SUPPRESSED"

ALLOWED_SOURCE_TYPES = {"WEATHER", "ECONOMIC", "CRYPTO", "SPORTS", "GENERAL", "EXAMPLE"}
DEFAULT_CATEGORY_PROBABILITIES = {
    "WEATHER": Decimal("0.55"),
    "ECONOMIC": Decimal("0.52"),
    "CRYPTO": Decimal("0.50"),
    "SPORTS": Decimal("0.50"),
    "GENERAL": Decimal("0.50"),
    "EXAMPLE": Decimal("0.50"),
}
DEFAULT_DENY_TERMS = {
    "assassination",
    "death of",
    "private medical",
    "insider",
    "nonpublic",
    "dox",
    "violence",
}


@dataclass(frozen=True)
class SyntheticMarketsConfig:
    enabled: bool = False
    mode: str = MODE_DISABLED
    configuration_version: str = CONFIGURATION_VERSION
    generation_policy_version: str = GENERATION_POLICY_VERSION
    listing_policy_version: str = LISTING_POLICY_VERSION
    model_routing_version: str = MODEL_ROUTING_VERSION
    constraint_policy_version: str = CONSTRAINT_POLICY_VERSION
    allow_exchange_write_endpoints: bool = False
    allow_order_actions: bool = False
    allow_opportunity_creation: bool = False
    approved_source_types: tuple[str, ...] = ("WEATHER", "ECONOMIC", "CRYPTO", "SPORTS", "GENERAL")
    max_candidates_per_run: int = 25
    max_contracts_per_event: int = 4
    max_horizon_days: int = 365
    require_live_complete: bool = True
    require_historical_scope: bool = True
    listing_stale_after_hours: int = 24
    probability_floor: Decimal = Decimal("0.01")
    probability_ceiling: Decimal = Decimal("0.99")
    coherence_tolerance: Decimal = Decimal("0.001")
    max_publishable_adjustment: Decimal = Decimal("0.10")
    internal_only: bool = True
    require_disclaimer: bool = True

    def validate(self) -> None:
        if self.mode not in OPERATING_MODES:
            raise ValueError(f"Unsupported Phase 3R mode: {self.mode}")
        if self.allow_exchange_write_endpoints or self.allow_order_actions:
            raise ValueError("Phase 3R cannot enable exchange writes or order actions.")
        if self.allow_opportunity_creation:
            raise ValueError("Phase 3R cannot create trading opportunities.")
        if self.max_candidates_per_run <= 0:
            raise ValueError("Phase 3R max_candidates_per_run must be positive.")
        if self.max_contracts_per_event <= 0:
            raise ValueError("Phase 3R max_contracts_per_event must be positive.")
        if self.max_horizon_days <= 0:
            raise ValueError("Phase 3R max_horizon_days must be positive.")
        if not Decimal("0") < self.probability_floor < self.probability_ceiling < Decimal("1"):
            raise ValueError("Phase 3R probability floor/ceiling must sit inside (0, 1).")
        unknown = set(self.approved_source_types) - ALLOWED_SOURCE_TYPES
        if unknown:
            raise ValueError(f"Unknown Phase 3R source types: {', '.join(sorted(unknown))}")


@dataclass(frozen=True)
class ObservationWindow:
    start_at: datetime
    end_at: datetime
    timezone: str = "UTC"

    def as_payload(self) -> dict[str, str]:
        return {
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "timezone": self.timezone,
        }


@dataclass(frozen=True)
class SettlementRule:
    settlement_rule_id: str
    settlement_rule_version: int
    primary_source_id: str
    primary_source_locator: str | None
    source_field: str
    revision_policy: str
    rounding_policy: str
    cancellation_policy: str
    postponement_policy: str
    rule_text: str
    rule_hash: str

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SyntheticEventSpec:
    synthetic_event_id: str
    synthetic_event_version: int
    semantic_hash: str
    canonical_title: str
    plain_language_summary: str
    category: str
    subcategory: str | None
    market_form: str
    observation_window: ObservationWindow
    mutually_exclusive: bool
    collectively_exhaustive: bool
    settlement_rule: SettlementRule
    generation_source: str
    status: str = EVENT_ACTIVE
    reason_codes: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "synthetic_event_id": self.synthetic_event_id,
            "synthetic_event_version": self.synthetic_event_version,
            "semantic_hash": self.semantic_hash,
            "canonical_title": self.canonical_title,
            "plain_language_summary": self.plain_language_summary,
            "category": self.category,
            "subcategory": self.subcategory,
            "market_form": self.market_form,
            "observation_window": self.observation_window.as_payload(),
            "mutually_exclusive": self.mutually_exclusive,
            "collectively_exhaustive": self.collectively_exhaustive,
            "settlement_rule": self.settlement_rule.as_payload(),
            "generation_source": self.generation_source,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class SyntheticContractSpec:
    synthetic_contract_id: str
    synthetic_contract_version: int
    synthetic_event_id: str
    canonical_question: str
    contract_type: str
    outcome_code: str
    condition: dict[str, Any]
    complement_contract_id: str | None = None
    constraint_group_id: str | None = None
    status: str = "ACTIVE"

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ListingMatch:
    match_id: str
    kalshi_series_ticker: str | None
    kalshi_event_ticker: str | None
    kalshi_market_ticker: str | None
    match_class: str
    semantic_score: Decimal
    logical_comparison: str
    field_differences: dict[str, Any]
    reviewer_status: str = "UNREVIEWED"
    effective_at: datetime | None = None

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["semantic_score"] = decimal_to_str(self.semantic_score)
        payload["effective_at"] = self.effective_at.isoformat() if self.effective_at else None
        return payload


@dataclass(frozen=True)
class ListingCheckResult:
    listing_check_id: str
    checked_at: datetime
    status: str
    pagination_complete: bool
    live_coverage_complete: bool
    historical_coverage_status: str
    historical_cutoff: datetime | None
    matches: tuple[ListingMatch, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "listing_check_id": self.listing_check_id,
            "checked_at": self.checked_at.isoformat(),
            "status": self.status,
            "pagination_complete": self.pagination_complete,
            "live_coverage_complete": self.live_coverage_complete,
            "historical_coverage_status": self.historical_coverage_status,
            "historical_cutoff": self.historical_cutoff.isoformat()
            if self.historical_cutoff
            else None,
            "matches": [match.as_payload() for match in self.matches],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ModelComponent:
    component_id: str
    model_id: str
    model_version: str
    calibration_id: str | None
    probability: Decimal
    weight: Decimal
    status: str = "USED"
    warnings: tuple[str, ...] = ()
    runtime_ms: int | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "calibration_id": self.calibration_id,
            "probability": decimal_to_str(self.probability),
            "weight": decimal_to_str(self.weight),
            "status": self.status,
            "warnings": list(self.warnings),
            "runtime_ms": self.runtime_ms,
        }


@dataclass(frozen=True)
class ProbabilityCard:
    card_id: str
    run_id: str
    synthetic_event: SyntheticEventSpec
    contracts: tuple[SyntheticContractSpec, ...]
    listing_check: ListingCheckResult
    estimate_id: str
    estimate_version: int
    estimate_as_of: datetime
    valid_until: datetime
    raw_probability: Decimal
    coherent_probability: Decimal
    interval: dict[str, Any]
    reliability: dict[str, str]
    model_components: tuple[ModelComponent, ...]
    constraint_result: dict[str, Any]
    assumptions: tuple[str, ...]
    missing_inputs: tuple[str, ...]
    drivers: tuple[str, ...]
    counterevidence: tuple[str, ...]
    status: str = ESTIMATE_PUBLISHED
    lineage: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

    def as_payload(self) -> dict[str, Any]:
        contract_payloads = []
        for contract in self.contracts:
            payload = contract.as_payload()
            payload["raw_probability"] = decimal_to_str(self.raw_probability)
            payload["coherent_probability"] = decimal_to_str(self.coherent_probability)
            payload["interval"] = self.interval
            payload["constraint_adjustment"] = decimal_to_str(
                self.coherent_probability - self.raw_probability
            )
            contract_payloads.append(payload)
        return {
            "schema_version": SCHEMA_VERSION,
            "card_id": self.card_id,
            "run_id": self.run_id,
            "synthetic_event": self.synthetic_event.as_payload(),
            "contracts": contract_payloads,
            "listing_check": self.listing_check.as_payload(),
            "estimate": {
                "estimate_id": self.estimate_id,
                "estimate_version": self.estimate_version,
                "estimate_as_of": self.estimate_as_of.isoformat(),
                "valid_until": self.valid_until.isoformat(),
                "status": self.status,
                "reliability": self.reliability,
                "model_components": [component.as_payload() for component in self.model_components],
                "constraint_result": self.constraint_result,
                "assumptions": list(self.assumptions),
                "missing_inputs": list(self.missing_inputs),
                "drivers": list(self.drivers),
                "counterevidence": list(self.counterevidence),
            },
            "governance": {
                "internal_research_only": True,
                "tradable": False,
                "exchange_eligibility": "UNKNOWN",
                "disclaimer": DISCLAIMER,
                "policy_status": "PASSED",
            },
            "lineage": self.lineage,
            "created_at": (self.created_at or self.estimate_as_of).isoformat(),
        }


@dataclass(frozen=True)
class SyntheticMarketsResult:
    run_id: str
    run_type: str
    mode: str
    status: str
    started_at: datetime
    completed_at: datetime
    cards: tuple[ProbabilityCard, ...]
    rejected_candidates: tuple[dict[str, Any], ...]
    listing_checks: tuple[ListingCheckResult, ...]
    markdown: str
    report_path: str | None
    json_path: str | None
    idempotent: bool = False

    @property
    def candidate_counts(self) -> dict[str, int]:
        return {
            "generated": len(self.cards) + len(self.rejected_candidates),
            "accepted": len(self.cards),
            "rejected": len(self.rejected_candidates),
        }

    @property
    def estimate_counts(self) -> dict[str, int]:
        return {"published_internal": len(self.cards), "suppressed": 0}


def stable_phase_3r_id(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kalshi_predictor:phase_3r:{text}"))


def canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), default=str)


def checksum_payload(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


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
