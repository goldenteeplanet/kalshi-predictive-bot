from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    RlBehaviorDecision,
    RlBehaviorPolicy,
    RlDatasetManifest,
    RlDriftSnapshot,
    RlHoldoutAccessLog,
    RlPolicyArtifact,
    RlPolicyDecision,
    RlPolicyEvaluation,
    RlPolicySegmentMetric,
    RlRewardDefinition,
    RlRewardLedger,
    RlRun,
)
from kalshi_predictor.reinforcement_learning.contracts import (
    ACTION_SPACE,
    BASELINE_POLICY_ID,
    BASELINE_POLICY_VERSION,
    CANDIDATE_POLICY_ID,
    CANDIDATE_POLICY_VERSION,
    RLEvaluationResult,
    canonical_json,
    stable_phase_3s_id,
)
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def existing_rl_run(session: Session, *, idempotency_key: str) -> RlRun | None:
    return session.scalar(
        select(RlRun).where(RlRun.idempotency_key == idempotency_key).limit(1)
    )


def latest_rl_run(session: Session) -> RlRun | None:
    return session.scalar(
        select(RlRun).order_by(desc(RlRun.completed_at), desc(RlRun.started_at)).limit(1)
    )


def rl_status(session: Session) -> dict[str, Any]:
    latest = latest_rl_run(session)
    return {
        "run_count": _count(session, RlRun),
        "dataset_count": _count(session, RlDatasetManifest),
        "reward_count": _count(session, RlRewardLedger),
        "evaluation_count": _count(session, RlPolicyEvaluation),
        "shadow_decision_count": _count(session, RlPolicyDecision),
        "drift_snapshot_count": _count(session, RlDriftSnapshot),
        "latest_run_id": latest.run_id if latest else None,
        "latest_status": latest.status if latest else "NOT_RUN",
        "latest_completed_at": latest.completed_at.isoformat()
        if latest and latest.completed_at
        else None,
    }


def persist_rl_evaluation_result(
    session: Session,
    *,
    result: RLEvaluationResult,
    idempotency_key: str,
    artifact_uris: dict[str, str | None],
) -> RlRun:
    now = utc_now()
    run = RlRun(
        run_id=result.run_id,
        run_type=result.run_type,
        formulation="CONTEXTUAL_BANDIT",
        mode=result.mode,
        status=result.status,
        started_at=result.created_at,
        completed_at=now,
        training_as_of=result.training_as_of,
        configuration_version="phase_3s_default_v1",
        reward_definition_id=result.reward_definition.reward_definition_id,
        reward_definition_version=result.reward_definition.reward_definition_version,
        baseline_policy_id=result.baseline_policy.policy_id,
        baseline_policy_version=result.baseline_policy.policy_version,
        candidate_policy_id=result.candidate_policy.policy_id,
        candidate_policy_version=result.candidate_policy.policy_version,
        dataset_manifest_id=result.dataset.dataset_manifest_id,
        source_watermarks_json=encode_json(result.dataset.source_watermarks),
        counts_json=encode_json(
            {
                "rows_total": result.dataset.rows_total,
                "rows_included": len(result.dataset.rows),
                "rows_excluded": sum(result.dataset.exclusion_counts.values()),
            }
        ),
        reason_codes_json=encode_json(list(result.reason_codes)),
        artifact_uris_json=encode_json(artifact_uris),
        idempotency_key=idempotency_key,
        raw_json=canonical_json(result.card),
    )
    session.add(run)
    session.flush()
    _persist_reward_definition(session, result=result, now=now)
    _persist_dataset(session, result=result, now=now)
    _persist_behavior_policy(session, result=result, now=now)
    _persist_policy_artifact(session, result=result, now=now)
    _persist_rows(session, result=result)
    _persist_evaluation(session, result=result, idempotency_key=idempotency_key, now=now)
    return run


def persist_shadow_decision(
    session: Session,
    *,
    policy_id: str,
    policy_version: str,
    mode: str,
    opportunity_id: str,
    decision_at: Any,
    recommended_action: str,
    baseline_action: str,
    valid_until: Any,
    value: dict[str, Any],
    support: dict[str, Any],
    reason_codes: list[str],
    idempotency_key: str,
) -> RlPolicyDecision:
    existing = session.scalar(
        select(RlPolicyDecision)
        .where(RlPolicyDecision.idempotency_key == idempotency_key)
        .limit(1)
    )
    if existing is not None:
        return existing
    row = RlPolicyDecision(
        policy_decision_id=stable_phase_3s_id("policy-decision", idempotency_key),
        policy_id=policy_id,
        policy_version=policy_version,
        mode=mode,
        opportunity_id=opportunity_id,
        decision_at=decision_at,
        recommended_action=recommended_action,
        baseline_action=baseline_action,
        valid_until=valid_until,
        value_json=canonical_json(value),
        support_json=canonical_json(support),
        reason_codes_json=encode_json(reason_codes),
        idempotency_key=idempotency_key,
        raw_json=canonical_json(
            {
                "policy_id": policy_id,
                "recommended_action": recommended_action,
                "baseline_action": baseline_action,
            }
        ),
    )
    session.add(row)
    session.flush()
    return row


def persist_drift_snapshot(
    session: Session,
    *,
    policy_id: str = CANDIDATE_POLICY_ID,
    policy_version: str = CANDIDATE_POLICY_VERSION,
    metrics: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> RlDriftSnapshot:
    now = utc_now()
    payload = {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "generated_at": now.isoformat(),
        "metrics": metrics or {},
        "warnings": warnings or [],
    }
    row = RlDriftSnapshot(
        drift_snapshot_id=stable_phase_3s_id("drift", canonical_json(payload)),
        policy_id=policy_id,
        policy_version=policy_version,
        generated_at=now,
        window_start_at=None,
        window_end_at=None,
        status="NOT_AVAILABLE" if not metrics else "RECORDED",
        metrics_json=canonical_json(metrics or {}),
        warnings_json=encode_json(warnings or ["No approved policy is active."]),
        raw_json=canonical_json(payload),
    )
    session.add(row)
    session.flush()
    return row


def _persist_reward_definition(
    session: Session,
    *,
    result: RLEvaluationResult,
    now: Any,
) -> None:
    definition = result.reward_definition
    if session.get(
        RlRewardDefinition,
        (definition.reward_definition_id, definition.reward_definition_version),
    ):
        return
    session.add(
        RlRewardDefinition(
            reward_definition_id=definition.reward_definition_id,
            reward_definition_version=definition.reward_definition_version,
            primary_metric=definition.primary_metric,
            roi_denominator=definition.roi_denominator,
            evidence_scope=definition.evidence_scope,
            cost_basis=definition.cost_basis,
            clipping_policy_json=canonical_json(
                {"min": definition.clip_min, "max": definition.clip_max}
            ),
            coefficient_json=canonical_json({"net_roi": "1"}),
            created_at=now,
            raw_json=canonical_json(definition.as_payload()),
        )
    )


def _persist_dataset(session: Session, *, result: RLEvaluationResult, now: Any) -> None:
    dataset = result.dataset
    if session.get(RlDatasetManifest, dataset.dataset_manifest_id) is None:
        session.add(
            RlDatasetManifest(
                dataset_manifest_id=dataset.dataset_manifest_id,
                run_id=result.run_id,
                dataset_hash=dataset.dataset_hash,
                training_as_of=dataset.training_as_of,
                rows_total=dataset.rows_total,
                rows_included=len(dataset.rows),
                rows_excluded=sum(dataset.exclusion_counts.values()),
                action_counts_json=encode_json(dataset.action_counts),
                evidence_counts_json=encode_json(dataset.evidence_counts),
                exclusion_counts_json=encode_json(dataset.exclusion_counts),
                source_watermarks_json=encode_json(dataset.source_watermarks),
                feature_schema_id=dataset.feature_schema_id,
                feature_schema_version=dataset.feature_schema_version,
                created_at=now,
                raw_json=canonical_json(dataset.as_payload()),
            )
        )


def _persist_behavior_policy(session: Session, *, result: RLEvaluationResult, now: Any) -> None:
    key = (BASELINE_POLICY_ID, BASELINE_POLICY_VERSION)
    if session.get(RlBehaviorPolicy, key) is None:
        session.add(
            RlBehaviorPolicy(
                behavior_policy_id=BASELINE_POLICY_ID,
                behavior_policy_version=BASELINE_POLICY_VERSION,
                policy_family="DETERMINISTIC_THRESHOLD",
                status="BASELINE_ACTIVE",
                action_space_json=encode_json(list(ACTION_SPACE)),
                artifact_hash=result.baseline_policy.artifact_hash,
                created_at=now,
                raw_json=canonical_json(result.baseline_policy.as_payload()),
            )
        )


def _persist_policy_artifact(session: Session, *, result: RLEvaluationResult, now: Any) -> None:
    artifact_id = stable_phase_3s_id(
        "policy-artifact",
        result.candidate_policy.policy_id,
        result.candidate_policy.policy_version,
    )
    if session.get(RlPolicyArtifact, artifact_id) is None:
        session.add(
            RlPolicyArtifact(
                policy_artifact_id=artifact_id,
                policy_id=result.candidate_policy.policy_id,
                policy_version=result.candidate_policy.policy_version,
                policy_family=result.candidate_policy.policy_family,
                status=result.candidate_policy.status,
                artifact_hash=result.candidate_policy.artifact_hash or "",
                training_run_id=result.run_id,
                dataset_manifest_id=result.dataset.dataset_manifest_id,
                action_space_json=encode_json(list(ACTION_SPACE)),
                parameters_json=canonical_json(result.card.get("candidate_policy", {})),
                lineage_json=canonical_json(result.card.get("lineage", {})),
                created_at=now,
                raw_json=canonical_json(result.candidate_policy.as_payload()),
            )
        )


def _persist_rows(session: Session, *, result: RLEvaluationResult) -> None:
    for row in result.dataset.rows:
        if session.get(RlBehaviorDecision, row.decision_id) is None:
            session.add(
                RlBehaviorDecision(
                    decision_id=row.decision_id,
                    dataset_manifest_id=result.dataset.dataset_manifest_id,
                    opportunity_id=row.opportunity_id,
                    forecast_id=row.forecast_id,
                    instrument_id=row.instrument_id,
                    category_id=row.category_id,
                    model_id=row.model_id,
                    decision_at=row.decision_at,
                    chosen_action=row.chosen_action,
                    action_set_json=encode_json(list(row.action_set)),
                    action_mask_json=canonical_json(row.action_mask),
                    propensity_json=canonical_json(row.propensities),
                    propensity_quality=row.propensity_quality,
                    behavior_policy_id=row.behavior_policy_id,
                    behavior_policy_version=row.behavior_policy_version,
                    feature_values_json=canonical_json(row.feature_values),
                    reason_codes_json=encode_json(list(row.reason_codes)),
                    raw_json=canonical_json(row.as_payload()),
                )
            )
        reward_id = stable_phase_3s_id("reward", result.run_id, row.decision_id)
        if session.get(RlRewardLedger, reward_id) is None:
            session.add(
                RlRewardLedger(
                    reward_id=reward_id,
                    run_id=result.run_id,
                    dataset_manifest_id=result.dataset.dataset_manifest_id,
                    decision_id=row.decision_id,
                    opportunity_id=row.opportunity_id,
                    forecast_id=row.forecast_id,
                    trade_id=row.trade_id,
                    action=row.chosen_action,
                    evidence_type=row.evidence_type,
                    reward_status=row.reward_status,
                    reward_definition_id=result.reward_definition.reward_definition_id,
                    reward_definition_version=result.reward_definition.reward_definition_version,
                    decision_at=row.decision_at,
                    reward_finalized_at=result.created_at,
                    gross_pnl=decimal_to_str(row.gross_pnl),
                    net_pnl=decimal_to_str(row.net_pnl),
                    total_cost=decimal_to_str(row.total_cost),
                    roi_denominator=decimal_to_str(row.roi_denominator),
                    raw_reward=decimal_to_str(row.raw_reward),
                    transformed_reward=decimal_to_str(row.reward),
                    normalized_reward=decimal_to_str(row.reward),
                    reason_codes_json=encode_json(list(row.reason_codes)),
                    supersedes_reward_id=None,
                    idempotency_key=f"phase3s:reward:{row.decision_id}:{result.run_id}",
                    raw_json=canonical_json(row.as_payload()),
                )
            )


def _persist_evaluation(
    session: Session,
    *,
    result: RLEvaluationResult,
    idempotency_key: str,
    now: Any,
) -> None:
    if session.get(RlPolicyEvaluation, result.evaluation_id) is None:
        session.add(
            RlPolicyEvaluation(
                evaluation_id=result.evaluation_id,
                run_id=result.run_id,
                dataset_manifest_id=result.dataset.dataset_manifest_id,
                candidate_policy_id=result.candidate_policy.policy_id,
                candidate_policy_version=result.candidate_policy.policy_version,
                baseline_policy_id=result.baseline_policy.policy_id,
                baseline_policy_version=result.baseline_policy.policy_version,
                evaluation_status=result.status,
                recommendation_status=result.recommendation_status,
                evidence_scope=result.reward_definition.evidence_scope,
                estimator_results_json=canonical_json(
                    [estimator.as_payload() for estimator in result.estimator_results]
                ),
                economic_metrics_json=canonical_json(result.economic_metrics),
                risk_metrics_json=canonical_json(result.risk_metrics),
                behavior_support_json=canonical_json(result.behavior_support),
                acceptance_gates_json=canonical_json(list(result.acceptance_gates)),
                card_json=canonical_json(result.card),
                created_at=now,
                idempotency_key=f"phase3s:evaluation:{idempotency_key}",
                raw_json=canonical_json(result.card),
            )
        )
    for gate in result.acceptance_gates:
        segment_id = stable_phase_3s_id("segment", result.evaluation_id, gate["gate_id"])
        if session.get(RlPolicySegmentMetric, segment_id) is None:
            session.add(
                RlPolicySegmentMetric(
                    segment_metric_id=segment_id,
                    evaluation_id=result.evaluation_id,
                    segment_name="gate",
                    segment_value=str(gate["gate_id"]),
                    sample_count=len(result.dataset.rows),
                    candidate_value=result.economic_metrics.get("candidate_value"),
                    baseline_value=result.economic_metrics.get("baseline_value"),
                    improvement=result.economic_metrics.get("improvement"),
                    support_status="SUPPORTED" if gate.get("result") else "LIMITED",
                    raw_json=canonical_json(gate),
                )
            )
    if result.status == "COMPLETED":
        holdout_id = stable_phase_3s_id("holdout", result.run_id, "not-used")
        if session.get(RlHoldoutAccessLog, holdout_id) is None:
            session.add(
                RlHoldoutAccessLog(
                    holdout_access_id=holdout_id,
                    run_id=result.run_id,
                    accessed_at=now,
                    holdout_policy_id="phase_3s_no_protected_holdout_v1",
                    reason="Protected holdout not used by default Phase 3S evaluation.",
                    raw_json=canonical_json({"run_id": result.run_id, "used": False}),
                )
            )


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)
