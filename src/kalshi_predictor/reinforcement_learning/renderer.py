from __future__ import annotations

from kalshi_predictor.reinforcement_learning.contracts import RLEvaluationResult


def render_rl_policy_markdown(result: RLEvaluationResult) -> str:
    estimator = result.estimator_results[0] if result.estimator_results else None
    improvement = estimator.improvement if estimator else None
    lower = estimator.lower_bound if estimator else None
    upper = estimator.upper_bound if estimator else None
    candidate_name = (
        f"{result.candidate_policy.policy_id}@{result.candidate_policy.policy_version}"
    )
    baseline_name = f"{result.baseline_policy.policy_id}@{result.baseline_policy.policy_version}"
    reward_name = (
        f"{result.reward_definition.reward_definition_id}@"
        f"{result.reward_definition.reward_definition_version}"
    )
    lines = [
        "# Phase 3S Policy Evaluation Report",
        "",
        "Policies that improved net economic value",
        "Policies that looked profitable but failed support or risk checks",
        "Where the current policy works and fails",
        "Behavior-policy and propensity coverage",
        "Shadow-policy decisions and disagreements",
        "Reward, data-quality, and regime-change warnings",
        "Recommended governed experiments",
        "",
        "## Executive summary",
        "",
        f"- Recommendation: {result.recommendation_status}",
        f"- Evaluation status: {result.status}",
        f"- Evidence scope: {result.reward_definition.evidence_scope}",
        f"- Training as of: {result.training_as_of.isoformat()}",
        f"- Dataset rows: {len(result.dataset.rows)} / {result.dataset.rows_total}",
        "- Estimated improvement over baseline: "
        f"{improvement if improvement is not None else 'n/a'}",
        "- 95% confidence interval: "
        f"{lower if lower is not None else 'n/a'} to "
        f"{upper if upper is not None else 'n/a'}",
        "",
        "## Candidate and baseline policy",
        "",
        f"- Candidate: `{candidate_name}`",
        f"- Baseline: `{baseline_name}`",
        "- Formulation: CONTEXTUAL_BANDIT",
        "- Actions: SKIP, PROCEED",
        "- Phase 3S returns no quantity and cannot create orders.",
        "",
        "## Economic value after costs",
        "",
        f"- Candidate value: {result.economic_metrics.get('candidate_value')}",
        f"- Baseline value: {result.economic_metrics.get('baseline_value')}",
        f"- Improvement: {result.economic_metrics.get('improvement')}",
        f"- Proceed rate: {result.economic_metrics.get('proceed_rate')}",
        "",
        "## Risk and drawdown comparison",
        "",
        "- Risk metrics are reported as NOT_AVAILABLE until enough finalized reward "
        "history exists.",
        "- Phase 3M remains sizing authority.",
        "- Phase 3N remains final risk authority.",
        "",
        "## Behavior-policy and action-support audit",
        "",
        f"- Complete action sets: {result.behavior_support.get('complete_action_set_rate')}",
        f"- Support coverage: {result.behavior_support.get('candidate_action_support_coverage')}",
        f"- Unsupported action rate: {result.behavior_support.get('unsupported_action_rate')}",
        f"- Maximum importance weight: {result.behavior_support.get('maximum_importance_weight')}",
        "",
        "## Off-policy estimator results",
        "",
        "| Estimator | Status | Candidate | Baseline | Improvement | Interval | ESS | Warnings |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in result.estimator_results:
        payload = row.as_payload()
        interval = payload["confidence_interval"]
        lines.append(
            "| "
            f"{payload['estimator']} | {payload['status']} | {payload['candidate_value']} | "
            f"{payload['baseline_value']} | {payload['improvement']} | "
            f"{interval['lower']} to {interval['upper']} | {payload['effective_sample_size']} | "
            f"{'; '.join(payload['warnings'])} |"
        )
    lines.extend(
        [
            "",
            "## Temporal validation",
            "",
            "- Primary validation is time ordered by `training_as_of`.",
            "- Protected holdout is not used by the default research command.",
            "",
            "## Segment performance",
            "",
            "- Segment metrics are persisted for acceptance gates; broader segment claims "
            "require more data.",
            "",
            "## Stress tests",
            "",
            "- Not applicable until minimum finalized reward support is met.",
            "",
            "## Shadow disagreements",
            "",
            "- Shadow recommendations are logged only through explicit shadow/report commands.",
            "- A shadow PROCEED is not a realized trade when the baseline skipped it.",
            "",
            "## Failure cases and OOD fallbacks",
            "",
            "- Missing, stale, invalid, or unsupported state falls back to baseline.",
            "",
            "## What changed",
            "",
            "- Phase 3S evaluation was generated from the current Phase 3O memory snapshot.",
            "",
            "## Recommendation and required approvals",
            "",
            f"- Recommendation: {result.recommendation_status}",
            "- Human review required before any production influence.",
            "- Online exploration enabled: false",
            "- Governed gate enabled by default: false",
            "",
            "## Lineage and reproducibility",
            "",
            f"- Dataset: `{result.dataset.dataset_manifest_id}`",
            f"- Dataset hash: `{result.dataset.dataset_hash}`",
            f"- Reward definition: `{reward_name}`",
            f"- Candidate artifact hash: `{result.candidate_policy.artifact_hash}`",
            f"- Reason codes: {', '.join(result.reason_codes) or 'none'}",
            "",
        ]
    )
    return "\n".join(lines)
