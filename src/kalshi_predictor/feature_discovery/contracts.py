from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import decimal_to_str

SCHEMA_VERSION = "3Q.1"
CONFIGURATION_VERSION = "phase_3q_default_v1"
CANDIDATE_GRAMMAR_VERSION = "phase_3q_grammar_v1"
EVALUATION_POLICY_ID = "phase_3q_paired_temporal_v1"
STATISTICAL_POLICY_ID = "phase_3q_bh_qvalue_v1"
HOLDOUT_POLICY_ID = "phase_3q_governed_holdout_v1"
OUTCOME_SPEC_VERSION = "phase_3q_outcomes_v1"
METRIC_CATALOG_VERSION = "phase_3q_metrics_v1"

RUN_INCREMENTAL = "INCREMENTAL"
RUN_FULL_SEARCH = "FULL_SEARCH"
RUN_BACKFILL = "BACKFILL"
RUN_ON_DEMAND = "ON_DEMAND"
RUN_REPLAY = "REPLAY"
RUN_TYPES = {RUN_INCREMENTAL, RUN_FULL_SEARCH, RUN_BACKFILL, RUN_ON_DEMAND, RUN_REPLAY}

MODE_DISABLED = "disabled"
MODE_SHADOW_RESEARCH = "shadow_research"
MODE_GOVERNED_RESEARCH = "governed_research"
OPERATING_MODES = {MODE_DISABLED, MODE_SHADOW_RESEARCH, MODE_GOVERNED_RESEARCH}

STATUS_VALIDATED = "VALIDATED"
STATUS_WATCHLIST = "WATCHLIST"
STATUS_REJECTED = "REJECTED"
STATUS_NOT_EVALUATED = "NOT_EVALUATED"

ACTION_NO_ACTION = "NO_ACTION"
ACTION_COLLECT_MORE_DATA = "COLLECT_MORE_DATA"
ACTION_INVESTIGATE_DATA_QUALITY = "INVESTIGATE_DATA_QUALITY"
ACTION_RUN_TARGETED_BACKTEST = "RUN_TARGETED_BACKTEST"
ACTION_RUN_SHADOW_EXPERIMENT = "RUN_SHADOW_EXPERIMENT"
ACTION_ADD_TO_OFFLINE_MODEL_EXPERIMENT = "ADD_TO_OFFLINE_MODEL_EXPERIMENT"
ACTION_REVIEW_FOR_DEPRECATION = "REVIEW_FOR_DEPRECATION"
RECOMMENDATION_ACTIONS = {
    ACTION_NO_ACTION,
    ACTION_COLLECT_MORE_DATA,
    ACTION_INVESTIGATE_DATA_QUALITY,
    ACTION_RUN_TARGETED_BACKTEST,
    ACTION_RUN_SHADOW_EXPERIMENT,
    ACTION_ADD_TO_OFFLINE_MODEL_EXPERIMENT,
    ACTION_REVIEW_FOR_DEPRECATION,
}

ALLOWED_FEATURE_SOURCES = {
    "predicted_probability",
    "confidence_score",
    "opportunity_score",
    "liquidity_score",
    "risk_adjusted_expected_value",
    "phase_3m_composite_score",
    "phase_3m_proposed_contracts",
    "phase_3n_approved_contracts",
    "spread_bps",
    "volume",
    "open_interest",
    "executable_liquidity_contracts",
}

FORBIDDEN_SOURCE_TOKENS = {
    "actual_value",
    "actual_return",
    "brier_component",
    "direction_correct",
    "exit_price",
    "forecast_error",
    "gross_pnl",
    "label",
    "max_adverse_excursion",
    "max_favorable_excursion",
    "net_pnl",
    "outcome",
    "phase_3p",
    "post_entry",
    "realized_pnl",
    "settlement",
    "settled",
    "target",
}


@dataclass(frozen=True)
class FeatureDiscoveryConfig:
    operating_mode: str = MODE_SHADOW_RESEARCH
    configuration_version: str = CONFIGURATION_VERSION
    candidate_policy_id: str = "phase_3q_default_candidates_v1"
    candidate_grammar_version: str = CANDIDATE_GRAMMAR_VERSION
    evaluation_policy_id: str = EVALUATION_POLICY_ID
    statistical_policy_id: str = STATISTICAL_POLICY_ID
    holdout_policy_id: str = HOLDOUT_POLICY_ID
    data_mode: str = "AS_OBSERVED"
    min_samples: int = 5
    max_candidates: int = 50
    min_practical_effect: Decimal = Decimal("0.05")
    q_value_threshold: Decimal = Decimal("0.20")
    embargo_seconds: int = 0
    purge_seconds: int = 0
    max_interaction_depth: int = 1
    random_seed: int = 20260623
    allow_production_mutation: bool = False
    report_limit: int = 25

    def validate(self) -> None:
        if self.operating_mode not in OPERATING_MODES:
            raise ValueError(f"Unsupported Phase 3Q mode: {self.operating_mode}")
        if self.min_samples < 0:
            raise ValueError("Phase 3Q min_samples cannot be negative.")
        if self.max_candidates <= 0:
            raise ValueError("Phase 3Q max_candidates must be positive and bounded.")
        if self.embargo_seconds < 0 or self.purge_seconds < 0:
            raise ValueError("Phase 3Q purge and embargo windows cannot be negative.")
        if self.max_interaction_depth < 0:
            raise ValueError("Phase 3Q interaction depth cannot be negative.")
        if not Decimal("0") <= self.q_value_threshold <= Decimal("1"):
            raise ValueError("Phase 3Q q-value threshold must be in [0, 1].")
        if self.min_practical_effect < 0:
            raise ValueError("Phase 3Q practical effect threshold cannot be negative.")
        if self.allow_production_mutation:
            raise ValueError("Phase 3Q cannot enable automatic production mutation.")


@dataclass(frozen=True)
class SourceWatermarks:
    market_memory_latest: str | None
    forecast_memory_latest: str | None
    trade_memory_latest: str | None
    market_memory_rows: int
    forecast_memory_rows: int
    trade_memory_rows: int

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryDatasetRow:
    row_id: str
    analysis_unit: str
    source_memory_id: str
    instrument_id: str
    category_id: str | None
    model_id: str | None
    execution_mode: str
    decision_timestamp: datetime
    feature_observed_through: datetime
    label_available_at: datetime
    label_interval_start: datetime
    label_interval_end: datetime
    outcome_name: str
    outcome_value: Decimal
    net_pnl: Decimal | None
    total_cost: Decimal | None
    feature_values: dict[str, Decimal]
    feature_lineage: dict[str, Any] = field(default_factory=dict)
    source_quality_flags: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["decision_timestamp"] = self.decision_timestamp.isoformat()
        payload["feature_observed_through"] = self.feature_observed_through.isoformat()
        payload["label_available_at"] = self.label_available_at.isoformat()
        payload["label_interval_start"] = self.label_interval_start.isoformat()
        payload["label_interval_end"] = self.label_interval_end.isoformat()
        payload["outcome_value"] = decimal_to_str(self.outcome_value)
        payload["net_pnl"] = decimal_to_str(self.net_pnl) if self.net_pnl is not None else None
        payload["total_cost"] = (
            decimal_to_str(self.total_cost) if self.total_cost is not None else None
        )
        payload["feature_values"] = {
            key: decimal_to_str(value) for key, value in self.feature_values.items()
        }
        return payload


@dataclass(frozen=True)
class DatasetManifest:
    manifest_id: str
    manifest_hash: str
    training_as_of: datetime
    data_mode: str
    rows_total: int
    rows_included: int
    excluded_counts: dict[str, int]
    source_watermarks: SourceWatermarks
    unavailable_sources: list[str]

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["training_as_of"] = self.training_as_of.isoformat()
        payload["source_watermarks"] = self.source_watermarks.as_payload()
        return payload


@dataclass(frozen=True)
class CandidateDefinition:
    candidate_id: str
    feature_definition_id: str
    feature_name: str
    feature_family: str
    expression: dict[str, Any]
    source_fields: tuple[str, ...]
    origin: str = "RAW"
    parent_candidate_ids: tuple[str, ...] = ()
    lineage: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "feature_definition_id": self.feature_definition_id,
            "feature_name": self.feature_name,
            "feature_family": self.feature_family,
            "expression": self.expression,
            "source_fields": list(self.source_fields),
            "origin": self.origin,
            "parent_candidate_ids": list(self.parent_candidate_ids),
            "lineage": self.lineage,
        }


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate: CandidateDefinition
    status: str
    reason_codes: list[str]
    sample_size: int
    baseline_rate: Decimal | None
    candidate_rate: Decimal | None
    paired_delta: Decimal | None
    economic_effect: Decimal | None
    stability_score: Decimal
    q_value: Decimal | None
    composite_score: Decimal
    fold_results: list[dict[str, Any]]
    segment_results: list[dict[str, Any]]
    relationship_notes: list[dict[str, Any]]
    recommendation_action: str

    def scorecard_payload(self, run_id: str, training_as_of: datetime) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "training_as_of": training_as_of.isoformat(),
            "candidate": self.candidate.as_payload(),
            "status": self.status,
            "reason_codes": self.reason_codes,
            "target": {
                "outcome_name": "net_profitable_after_costs",
                "analysis_unit": "FORECAST_OR_TRADE",
                "outcome_source": "Phase 3O memory finalized labels",
            },
            "metrics": {
                "sample_size": self.sample_size,
                "baseline_rate": _decimal_or_none(self.baseline_rate),
                "candidate_rate": _decimal_or_none(self.candidate_rate),
                "paired_delta": _decimal_or_none(self.paired_delta),
                "economic_effect_net_pnl_per_contract": _decimal_or_none(self.economic_effect),
                "stability_score": _decimal_or_none(self.stability_score),
                "q_value": _decimal_or_none(self.q_value),
                "composite_score": _decimal_or_none(self.composite_score),
            },
            "fold_results": self.fold_results,
            "segment_results": self.segment_results,
            "relationship_notes": self.relationship_notes,
            "recommendation": {
                "action": self.recommendation_action,
                "recommendation_requires_human_review": (
                    self.recommendation_action != ACTION_NO_ACTION
                ),
            },
        }


@dataclass(frozen=True)
class FeatureDiscoveryResult:
    run_id: str
    run_type: str
    status: str
    training_as_of: datetime
    manifest: DatasetManifest
    candidate_evaluations: list[CandidateEvaluation]
    markdown: str
    report_path: str | None
    json_path: str | None
    idempotent: bool = False

    @property
    def candidate_counts(self) -> dict[str, int]:
        counts = {
            "generated": len(self.candidate_evaluations),
            "validated": 0,
            "watchlist": 0,
            "rejected": 0,
        }
        for evaluation in self.candidate_evaluations:
            if evaluation.status == STATUS_VALIDATED:
                counts["validated"] += 1
            elif evaluation.status == STATUS_WATCHLIST:
                counts["watchlist"] += 1
            elif evaluation.status == STATUS_REJECTED:
                counts["rejected"] += 1
        return counts


def stable_phase_3q_id(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kalshi_predictor:phase_3q:{text}"))


def canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), default=str)


def checksum_payload(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


def scorecard_checksum(scorecard: dict[str, Any]) -> str:
    return checksum_payload(scorecard)


def _decimal_or_none(value: Decimal | None) -> str | None:
    return decimal_to_str(value) if value is not None else None


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
