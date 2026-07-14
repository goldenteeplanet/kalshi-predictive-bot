from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.reinforcement_learning.contracts import (
    ACTION_PROCEED,
    ACTION_SKIP,
    BASELINE_POLICY_ID,
    BASELINE_POLICY_VERSION,
    CANDIDATE_POLICY_ID,
    CANDIDATE_POLICY_VERSION,
    MODE_DISABLED,
    MODE_SHADOW,
    RLConfig,
)
from kalshi_predictor.reinforcement_learning.repository import persist_shadow_decision
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class PolicyRecommendation:
    opportunity_id: str
    recommended_action: str
    baseline_action: str
    policy_id: str
    policy_version: str
    reason_codes: tuple[str, ...]
    value: dict[str, Any]
    support: dict[str, Any]
    valid_until: Any


def recommend_policy_action(
    *,
    opportunity: dict[str, Any],
    config: RLConfig,
    session: Session | None = None,
) -> PolicyRecommendation:
    now = utc_now()
    opportunity_id = str(
        opportunity.get("opportunity_id") or opportunity.get("ticker") or "unknown"
    )
    baseline = _baseline_action(opportunity, config=config)
    if config.mode == MODE_DISABLED:
        recommendation = PolicyRecommendation(
            opportunity_id=opportunity_id,
            recommended_action=baseline,
            baseline_action=baseline,
            policy_id=BASELINE_POLICY_ID,
            policy_version=BASELINE_POLICY_VERSION,
            reason_codes=("RL_DISABLED", "BASELINE_FALLBACK"),
            value={"expected_net_value": "NOT_AVAILABLE"},
            support={"status": "BASELINE_FALLBACK"},
            valid_until=now + timedelta(minutes=5),
        )
    else:
        candidate = _candidate_action(opportunity, config=config)
        reason = "RECOMMEND_PROCEED" if candidate == ACTION_PROCEED else "RECOMMEND_SKIP"
        recommendation = PolicyRecommendation(
            opportunity_id=opportunity_id,
            recommended_action=candidate,
            baseline_action=baseline,
            policy_id=CANDIDATE_POLICY_ID,
            policy_version=CANDIDATE_POLICY_VERSION,
            reason_codes=(reason, "SHADOW_ONLY") if config.mode == MODE_SHADOW else (reason,),
            value={
                "expected_net_value": str(
                    opportunity.get("risk_adjusted_expected_value") or "0"
                )
            },
            support={"status": "SUPPORTED", "ood": False},
            valid_until=now + timedelta(minutes=5),
        )
    if session is not None and config.mode == MODE_SHADOW:
        persist_shadow_decision(
            session,
            policy_id=recommendation.policy_id,
            policy_version=recommendation.policy_version,
            mode=config.mode,
            opportunity_id=opportunity_id,
            decision_at=now,
            recommended_action=recommendation.recommended_action,
            baseline_action=recommendation.baseline_action,
            valid_until=recommendation.valid_until,
            value=recommendation.value,
            support=recommendation.support,
            reason_codes=list(recommendation.reason_codes),
            idempotency_key=f"phase3s:shadow:{opportunity_id}:{now.isoformat()}",
        )
    return recommendation


def _baseline_action(opportunity: dict[str, Any], *, config: RLConfig) -> str:
    score = to_decimal(opportunity.get("opportunity_score")) or Decimal("0")
    return ACTION_PROCEED if score >= config.baseline_opportunity_score else ACTION_SKIP


def _candidate_action(opportunity: dict[str, Any], *, config: RLConfig) -> str:
    score = to_decimal(opportunity.get("opportunity_score")) or Decimal("0")
    confidence = to_decimal(opportunity.get("confidence_score")) or Decimal("0")
    if score >= config.candidate_opportunity_score and confidence >= Decimal("0.25"):
        return ACTION_PROCEED
    return ACTION_SKIP
