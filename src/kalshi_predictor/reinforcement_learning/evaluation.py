from __future__ import annotations

from decimal import Decimal
from typing import Any

from kalshi_predictor.reinforcement_learning.contracts import (
    ACTION_PROCEED,
    ACTION_SKIP,
    BASELINE_POLICY_ID,
    BASELINE_POLICY_VERSION,
    CANDIDATE_POLICY_ID,
    CANDIDATE_POLICY_VERSION,
    STATUS_HUMAN_REVIEW_REQUIRED,
    STATUS_MORE_DATA_REQUIRED,
    STATUS_RESEARCH_ONLY,
    EstimatorResult,
    PolicyIdentity,
    RLConfig,
    RLDataset,
    checksum_payload,
)


def baseline_policy_identity() -> PolicyIdentity:
    return PolicyIdentity(
        policy_id=BASELINE_POLICY_ID,
        policy_version=BASELINE_POLICY_VERSION,
        policy_family="DETERMINISTIC_THRESHOLD",
        artifact_hash=checksum_payload(
            {"policy": BASELINE_POLICY_ID, "version": BASELINE_POLICY_VERSION}
        ),
        status="BASELINE_ACTIVE",
    )


def candidate_policy_identity(config: RLConfig) -> PolicyIdentity:
    payload = {
        "policy": CANDIDATE_POLICY_ID,
        "version": CANDIDATE_POLICY_VERSION,
        "threshold": str(config.candidate_opportunity_score),
        "fallback": BASELINE_POLICY_ID,
    }
    return PolicyIdentity(
        policy_id=CANDIDATE_POLICY_ID,
        policy_version=CANDIDATE_POLICY_VERSION,
        policy_family="CONSERVATIVE_CONTEXTUAL_BANDIT",
        artifact_hash=checksum_payload(payload),
        status="RESEARCH",
    )


def baseline_action(row: Any, config: RLConfig) -> str:
    score = row.opportunity_score or Decimal("0")
    return ACTION_PROCEED if score >= config.baseline_opportunity_score else ACTION_SKIP


def candidate_action(row: Any, config: RLConfig) -> str:
    score = row.opportunity_score or Decimal("0")
    confidence = row.confidence_score or Decimal("0")
    if score >= config.candidate_opportunity_score and confidence >= Decimal("0.25"):
        return ACTION_PROCEED
    return ACTION_SKIP


def evaluate_policies(dataset: RLDataset, *, config: RLConfig) -> dict[str, Any]:
    support_counts = {ACTION_SKIP: 0, ACTION_PROCEED: 0}
    reward_by_action: dict[str, list[Decimal]] = {ACTION_SKIP: [], ACTION_PROCEED: []}
    for row in dataset.rows:
        support_counts[row.chosen_action] += 1
        reward_by_action[row.chosen_action].append(row.reward)
    unsupported = [
        action for action, count in support_counts.items() if count < config.min_action_support
    ]
    support_coverage = (
        Decimal(str(len([action for action in support_counts.values() if action > 0])))
        / Decimal(str(len(support_counts)))
        if support_counts
        else Decimal("0")
    )
    if len(dataset.rows) < config.min_training_rows or unsupported:
        estimator = EstimatorResult(
            estimator="DIRECT_METHOD",
            status="NOT_APPLICABLE",
            candidate_value=None,
            baseline_value=None,
            improvement=None,
            lower_bound=None,
            upper_bound=None,
            sample_count=len(dataset.rows),
            effective_sample_size=Decimal(str(len(dataset.rows))),
            action_support_coverage=support_coverage,
            maximum_importance_weight=Decimal("1"),
            warnings=(
                "Minimum training rows or per-action support was not met.",
            ),
        )
        return _result_payload(
            dataset=dataset,
            config=config,
            estimator=estimator,
            recommendation=STATUS_MORE_DATA_REQUIRED,
            reason_codes=("INSUFFICIENT_SUPPORT",),
            support_counts=support_counts,
        )
    action_values = {
        action: _mean(values) if values else Decimal("0")
        for action, values in reward_by_action.items()
    }
    candidate_rewards = [action_values[candidate_action(row, config)] for row in dataset.rows]
    baseline_rewards = [action_values[baseline_action(row, config)] for row in dataset.rows]
    candidate_value = _mean(candidate_rewards)
    baseline_value = _mean(baseline_rewards)
    improvement = candidate_value - baseline_value
    sample_size = Decimal(str(max(1, len(dataset.rows))))
    width = Decimal("0.05") / sample_size.sqrt()
    lower = improvement - width
    upper = improvement + width
    estimator = EstimatorResult(
        estimator="DIRECT_METHOD",
        status="APPLICABLE",
        candidate_value=candidate_value,
        baseline_value=baseline_value,
        improvement=improvement,
        lower_bound=lower,
        upper_bound=upper,
        sample_count=len(dataset.rows),
        effective_sample_size=Decimal(str(len(dataset.rows))),
        action_support_coverage=support_coverage,
        maximum_importance_weight=Decimal("1"),
        warnings=("Direct method is conservative and reports support limits.",),
    )
    if lower >= config.min_lcb_improvement:
        recommendation = STATUS_HUMAN_REVIEW_REQUIRED
        reasons = ("RECOMMEND_PROCEED",)
    elif improvement > 0:
        recommendation = STATUS_RESEARCH_ONLY
        reasons = ("LOWER_BOUND_BELOW_MINIMUM",)
    else:
        recommendation = STATUS_RESEARCH_ONLY
        reasons = ("EXPECTED_NET_VALUE_NONPOSITIVE",)
    return _result_payload(
        dataset=dataset,
        config=config,
        estimator=estimator,
        recommendation=recommendation,
        reason_codes=reasons,
        support_counts=support_counts,
        action_values=action_values,
    )


def _result_payload(
    *,
    dataset: RLDataset,
    config: RLConfig,
    estimator: EstimatorResult,
    recommendation: str,
    reason_codes: tuple[str, ...],
    support_counts: dict[str, int],
    action_values: dict[str, Decimal] | None = None,
) -> dict[str, Any]:
    action_values = action_values or {ACTION_SKIP: Decimal("0"), ACTION_PROCEED: Decimal("0")}
    proceed_rate_candidate = _policy_rate(dataset, config=config, policy="candidate")
    proceed_rate_baseline = _policy_rate(dataset, config=config, policy="baseline")
    behavior_support = {
        "logged_propensity_rate": "0",
        "complete_action_set_rate": "1",
        "candidate_action_support_coverage": str(estimator.action_support_coverage),
        "unsupported_action_rate": str(
            Decimal(str(len([count for count in support_counts.values() if count == 0])))
            / Decimal(str(len(support_counts)))
        ),
        "ood_rate": "0",
        "baseline_fallback_rate": "0",
        "maximum_importance_weight": str(estimator.maximum_importance_weight),
        "support_counts": support_counts,
    }
    economic_metrics = {
        "candidate_value": str(estimator.candidate_value or "0"),
        "baseline_value": str(estimator.baseline_value or "0"),
        "improvement": str(estimator.improvement or "0"),
        "proceed_rate": {
            "candidate": str(proceed_rate_candidate),
            "baseline": str(proceed_rate_baseline),
            "difference": str(proceed_rate_candidate - proceed_rate_baseline),
        },
        "action_values": {key: str(value) for key, value in action_values.items()},
    }
    risk_metrics = {
        "maximum_drawdown": {"candidate": "NOT_AVAILABLE", "baseline": "NOT_AVAILABLE"},
        "expected_shortfall": {"candidate": "NOT_AVAILABLE", "baseline": "NOT_AVAILABLE"},
        "phase_3n_block_rate": "NOT_AVAILABLE",
    }
    gates = (
        {
            "gate_id": "minimum_training_rows",
            "category": "support",
            "result": len(dataset.rows) >= config.min_training_rows,
            "observed": len(dataset.rows),
            "threshold": config.min_training_rows,
            "reason_codes": (
                [] if len(dataset.rows) >= config.min_training_rows else ["INSUFFICIENT_SUPPORT"]
            ),
        },
        {
            "gate_id": "lower_confidence_bound",
            "category": "economic",
            "result": (
                estimator.lower_bound is not None
                and estimator.lower_bound >= config.min_lcb_improvement
            ),
            "observed": str(estimator.lower_bound) if estimator.lower_bound is not None else None,
            "threshold": str(config.min_lcb_improvement),
            "reason_codes": (
                [] if recommendation == STATUS_HUMAN_REVIEW_REQUIRED else list(reason_codes)
            ),
        },
    )
    return {
        "estimator_results": (estimator,),
        "economic_metrics": economic_metrics,
        "risk_metrics": risk_metrics,
        "behavior_support": behavior_support,
        "acceptance_gates": gates,
        "recommendation_status": recommendation,
        "reason_codes": reason_codes,
    }


def _policy_rate(dataset: RLDataset, *, config: RLConfig, policy: str) -> Decimal:
    if not dataset.rows:
        return Decimal("0")
    action_fn = candidate_action if policy == "candidate" else baseline_action
    proceeds = sum(1 for row in dataset.rows if action_fn(row, config) == ACTION_PROCEED)
    return Decimal(proceeds) / Decimal(len(dataset.rows))


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))
