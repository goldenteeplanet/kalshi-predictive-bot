from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import decimal_to_str

SCHEMA_VERSION = "phase-3s.rl-policy-evaluation-card.v1"
CONFIGURATION_VERSION = "phase_3s_default_v1"
REWARD_DEFINITION_ID = "phase_3s_net_roi_v1"
REWARD_DEFINITION_VERSION = "1.0.0"
BASELINE_POLICY_ID = "current-opportunity-threshold"
BASELINE_POLICY_VERSION = "1.0.0"
CANDIDATE_POLICY_ID = "conservative-contextual-bandit"
CANDIDATE_POLICY_VERSION = "1.0.0"
FORMULATION = "CONTEXTUAL_BANDIT"

MODE_DISABLED = "disabled"
MODE_OFFLINE_REPLAY = "offline_replay"
MODE_SHADOW = "shadow"
MODE_ADVISORY = "advisory"
MODE_GOVERNED_GATE = "governed_gate"
OPERATING_MODES = {
    MODE_DISABLED,
    MODE_OFFLINE_REPLAY,
    MODE_SHADOW,
    MODE_ADVISORY,
    MODE_GOVERNED_GATE,
}

ACTION_SKIP = "SKIP"
ACTION_PROCEED = "PROCEED"
ACTION_DEFER = "DEFER"
ACTION_SPACE = (ACTION_SKIP, ACTION_PROCEED)

STATUS_DISABLED = "DISABLED"
STATUS_COMPLETED = "COMPLETED"
STATUS_MORE_DATA_REQUIRED = "MORE_DATA_REQUIRED"
STATUS_RESEARCH_ONLY = "RESEARCH_ONLY"
STATUS_HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"

EVIDENCE_LIVE = "LIVE_REALIZED"
EVIDENCE_PAPER = "PAPER_SIMULATED"
EVIDENCE_NO_ACTION = "NO_ACTION"
EVIDENCE_DOWNSTREAM_BLOCKED = "DOWNSTREAM_BLOCKED"

REASON_CODES = {
    "RL_DISABLED",
    "OFFLINE_ONLY",
    "SHADOW_ONLY",
    "POLICY_NOT_APPROVED",
    "POLICY_EXPIRED",
    "POLICY_ARTIFACT_INVALID",
    "POLICY_SCHEMA_MISMATCH",
    "BASELINE_FALLBACK",
    "INFERENCE_TIMEOUT",
    "FEATURE_VIEW_STALE",
    "FEATURE_MISSING",
    "QUOTE_STALE",
    "INVALID_STATE",
    "OOD_STATE",
    "INSUFFICIENT_SUPPORT",
    "ACTION_MASKED",
    "PROPENSITY_MISSING",
    "PROPENSITY_TOO_SMALL",
    "OVERLAP_INSUFFICIENT",
    "EXPECTED_NET_VALUE_NONPOSITIVE",
    "EXPECTED_ROI_BELOW_MINIMUM",
    "LOWER_BOUND_BELOW_MINIMUM",
    "UNCERTAINTY_TOO_HIGH",
    "RECOMMEND_PROCEED",
    "RECOMMEND_SKIP",
    "RECOMMEND_DEFER",
    "DOWNSTREAM_PHASE_3M_REJECTED",
    "DOWNSTREAM_PHASE_3N_REDUCED",
    "DOWNSTREAM_PHASE_3N_BLOCKED",
    "ORDER_NOT_CREATED",
    "NO_FILL",
    "PARTIAL_FILL",
    "REWARD_PROVISIONAL",
    "REWARD_INVALID",
    "HUMAN_OVERRIDE",
}


@dataclass(frozen=True)
class RLConfig:
    enabled: bool = False
    mode: str = MODE_DISABLED
    configuration_version: str = CONFIGURATION_VERSION
    min_training_rows: int = 25
    min_action_support: int = 3
    baseline_opportunity_score: Decimal = Decimal("45")
    candidate_opportunity_score: Decimal = Decimal("55")
    min_lcb_improvement: Decimal = Decimal("0.001")
    allow_online_exploration: bool = False
    governed_gate_enabled: bool = False
    max_decisions_per_run: int = 5000

    def validate(self) -> None:
        if self.mode not in OPERATING_MODES:
            raise ValueError(f"Unsupported Phase 3S mode: {self.mode}")
        if self.allow_online_exploration:
            raise ValueError("Phase 3S online exploration is disabled by default.")
        if self.governed_gate_enabled and self.mode != MODE_GOVERNED_GATE:
            raise ValueError("Governed gate requires PHASE_3S_MODE=governed_gate.")
        if self.mode == MODE_GOVERNED_GATE and not self.governed_gate_enabled:
            raise ValueError("Governed gate mode requires explicit enablement.")
        if self.min_training_rows < 0 or self.min_action_support < 0:
            raise ValueError("Phase 3S minimum row/support settings cannot be negative.")


@dataclass(frozen=True)
class RewardDefinition:
    reward_definition_id: str = REWARD_DEFINITION_ID
    reward_definition_version: str = REWARD_DEFINITION_VERSION
    primary_metric: str = "CLIPPED_NET_ROI"
    roi_denominator: str = "WORST_CASE_CAPITAL_AT_RISK"
    evidence_scope: str = "PAPER_AND_LIVE_SEPARATED"
    cost_basis: str = "NET_OF_ALL_AUTHORITATIVE_COSTS"
    clip_min: Decimal = Decimal("-5")
    clip_max: Decimal = Decimal("5")

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["clip_min"] = decimal_to_str(self.clip_min)
        payload["clip_max"] = decimal_to_str(self.clip_max)
        return payload


@dataclass(frozen=True)
class RLDatasetRow:
    decision_id: str
    decision_at: datetime
    chosen_action: str
    action_set: tuple[str, ...]
    action_mask: dict[str, bool]
    propensities: dict[str, str]
    propensity_quality: str
    behavior_policy_id: str
    behavior_policy_version: str
    opportunity_id: str | None
    forecast_id: str | None
    trade_id: str | None
    instrument_id: str
    category_id: str | None
    model_id: str | None
    opportunity_score: Decimal | None
    confidence_score: Decimal | None
    reward: Decimal
    raw_reward: Decimal
    gross_pnl: Decimal | None
    net_pnl: Decimal | None
    total_cost: Decimal | None
    roi_denominator: Decimal | None
    evidence_type: str
    reward_status: str
    reason_codes: tuple[str, ...]
    feature_values: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "opportunity_score",
            "confidence_score",
            "reward",
            "raw_reward",
            "gross_pnl",
            "net_pnl",
            "total_cost",
            "roi_denominator",
        ):
            payload[key] = decimal_to_str(payload[key])
        payload["decision_at"] = self.decision_at.isoformat()
        return payload


@dataclass(frozen=True)
class RLDataset:
    dataset_manifest_id: str
    dataset_hash: str
    training_as_of: datetime
    rows: tuple[RLDatasetRow, ...]
    rows_total: int
    exclusion_counts: dict[str, int]
    source_watermarks: dict[str, str | None]
    feature_schema_id: str = "phase_3o_forecast_trade_memory"
    feature_schema_version: str = "1.0.0"

    @property
    def action_counts(self) -> dict[str, int]:
        return dict(Counter(row.chosen_action for row in self.rows))

    @property
    def evidence_counts(self) -> dict[str, int]:
        return dict(Counter(row.evidence_type for row in self.rows))

    def as_payload(self) -> dict[str, Any]:
        return {
            "dataset_manifest_id": self.dataset_manifest_id,
            "dataset_hash": self.dataset_hash,
            "training_as_of": self.training_as_of.isoformat(),
            "rows_total": self.rows_total,
            "rows_included": len(self.rows),
            "rows_excluded": sum(self.exclusion_counts.values()),
            "action_counts": self.action_counts,
            "evidence_counts": self.evidence_counts,
            "exclusion_counts": self.exclusion_counts,
            "source_watermarks": self.source_watermarks,
            "feature_schema_id": self.feature_schema_id,
            "feature_schema_version": self.feature_schema_version,
        }


@dataclass(frozen=True)
class PolicyIdentity:
    policy_id: str
    policy_version: str
    policy_family: str
    artifact_hash: str | None
    status: str

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EstimatorResult:
    estimator: str
    status: str
    candidate_value: Decimal | None
    baseline_value: Decimal | None
    improvement: Decimal | None
    lower_bound: Decimal | None
    upper_bound: Decimal | None
    sample_count: int
    effective_sample_size: Decimal
    action_support_coverage: Decimal
    maximum_importance_weight: Decimal
    warnings: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "estimator": self.estimator,
            "status": self.status,
            "candidate_value": decimal_to_str(self.candidate_value),
            "baseline_value": decimal_to_str(self.baseline_value),
            "improvement": decimal_to_str(self.improvement),
            "confidence_interval": {
                "level": "0.95",
                "lower": decimal_to_str(self.lower_bound),
                "upper": decimal_to_str(self.upper_bound),
                "method": "deterministic_conservative_bound",
            },
            "sample_count": self.sample_count,
            "effective_sample_size": decimal_to_str(self.effective_sample_size),
            "action_support_coverage": decimal_to_str(self.action_support_coverage),
            "maximum_importance_weight": decimal_to_str(self.maximum_importance_weight),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RLEvaluationResult:
    run_id: str
    run_type: str
    mode: str
    status: str
    evaluation_id: str
    created_at: datetime
    training_as_of: datetime
    dataset: RLDataset
    reward_definition: RewardDefinition
    candidate_policy: PolicyIdentity
    baseline_policy: PolicyIdentity
    estimator_results: tuple[EstimatorResult, ...]
    economic_metrics: dict[str, Any]
    risk_metrics: dict[str, Any]
    behavior_support: dict[str, Any]
    acceptance_gates: tuple[dict[str, Any], ...]
    recommendation_status: str
    reason_codes: tuple[str, ...]
    card: dict[str, Any]
    markdown: str
    report_path: str | None
    json_path: str | None
    idempotent: bool = False


def stable_phase_3s_id(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kalshi_predictor:phase_3s:{text}"))


def canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), default=str)


def checksum_payload(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


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
