from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    FeatureCandidate,
    FeatureDiscoveryRun,
    FeatureEvaluation,
    FeatureFoldResult,
    FeatureHoldoutAccess,
    FeatureRecommendation,
    FeatureRelationship,
    FeatureSegmentResult,
)
from kalshi_predictor.feature_discovery.contracts import (
    ACTION_NO_ACTION,
    EVALUATION_POLICY_ID,
    HOLDOUT_POLICY_ID,
    FeatureDiscoveryResult,
    stable_phase_3q_id,
)
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def existing_discovery_run(session: Session, *, idempotency_key: str) -> FeatureDiscoveryRun | None:
    return session.scalar(
        select(FeatureDiscoveryRun)
        .where(FeatureDiscoveryRun.idempotency_key == idempotency_key)
        .limit(1)
    )


def latest_discovery_run(session: Session) -> FeatureDiscoveryRun | None:
    return session.scalar(
        select(FeatureDiscoveryRun)
        .order_by(desc(FeatureDiscoveryRun.completed_at), desc(FeatureDiscoveryRun.started_at))
        .limit(1)
    )


def feature_discovery_status(session: Session) -> dict[str, Any]:
    latest = latest_discovery_run(session)
    run_count = int(session.scalar(select(func.count()).select_from(FeatureDiscoveryRun)) or 0)
    candidate_count = int(
        session.scalar(select(func.count()).select_from(FeatureCandidate)) or 0
    )
    evaluation_count = int(
        session.scalar(select(func.count()).select_from(FeatureEvaluation)) or 0
    )
    return {
        "run_count": run_count,
        "candidate_count": candidate_count,
        "evaluation_count": evaluation_count,
        "recommendation_count": int(
            session.scalar(select(func.count()).select_from(FeatureRecommendation)) or 0
        ),
        "latest_run_id": latest.run_id if latest else None,
        "latest_status": latest.status if latest else "NOT_RUN",
        "latest_completed_at": (
            latest.completed_at.isoformat() if latest and latest.completed_at else None
        ),
    }


def persist_feature_discovery_result(
    session: Session,
    *,
    result: FeatureDiscoveryResult,
    idempotency_key: str,
    artifact_uris: dict[str, str | None],
) -> FeatureDiscoveryRun:
    now = utc_now()
    run = FeatureDiscoveryRun(
        run_id=result.run_id,
        run_type=result.run_type,
        status=result.status,
        requested_at=result.training_as_of,
        started_at=result.training_as_of,
        completed_at=now,
        training_as_of=result.training_as_of,
        source_watermarks_json=encode_json(result.manifest.source_watermarks.as_payload()),
        source_manifest_id=result.manifest.manifest_id,
        dataset_spec_hash=result.manifest.manifest_hash,
        candidate_policy_id="phase_3q_default_candidates_v1",
        candidate_grammar_version="phase_3q_grammar_v1",
        evaluation_policy_id=EVALUATION_POLICY_ID,
        statistical_policy_id="phase_3q_bh_qvalue_v1",
        holdout_policy_id=HOLDOUT_POLICY_ID,
        code_commit_sha=None,
        configuration_version="phase_3q_default_v1",
        random_seed_manifest_json=encode_json({"seed": 20260623}),
        candidate_counts_json=encode_json(result.candidate_counts),
        failure_reason_codes_json=encode_json(
            [] if result.status == "COMPLETED" else [result.status]
        ),
        artifact_uris_json=encode_json(artifact_uris),
        idempotency_key=idempotency_key,
        raw_json=encode_json(
            {
                "manifest": result.manifest.as_payload(),
                "candidate_counts": result.candidate_counts,
                "artifact_uris": artifact_uris,
            }
        ),
    )
    session.add(run)
    session.flush()
    _persist_candidates_and_evaluations(session, result=result, created_at=now)
    return run


def record_holdout_access(
    session: Session,
    *,
    run_id: str,
    candidate_batch_id: str,
    reason: str,
) -> FeatureHoldoutAccess:
    row = FeatureHoldoutAccess(
        run_id=run_id,
        candidate_batch_id=candidate_batch_id,
        accessed_at=utc_now(),
        holdout_policy_id=HOLDOUT_POLICY_ID,
        reason=reason,
        raw_json=encode_json(
            {
                "run_id": run_id,
                "candidate_batch_id": candidate_batch_id,
                "reason": reason,
                "policy": HOLDOUT_POLICY_ID,
            }
        ),
    )
    session.add(row)
    session.flush()
    return row


def _persist_candidates_and_evaluations(
    session: Session,
    *,
    result: FeatureDiscoveryResult,
    created_at: datetime,
) -> None:
    for evaluation in result.candidate_evaluations:
        candidate = evaluation.candidate
        existing_candidate = session.get(FeatureCandidate, candidate.candidate_id)
        if existing_candidate is None:
            row = FeatureCandidate(
                candidate_id=candidate.candidate_id,
                feature_definition_id=candidate.feature_definition_id,
                candidate_batch_id=result.run_id,
                parent_candidate_ids_json=encode_json(list(candidate.parent_candidate_ids)),
                origin=candidate.origin,
                created_by_run_id=result.run_id,
                status=evaluation.status,
                status_reason_codes_json=encode_json(evaluation.reason_codes),
                supersedes_candidate_id=None,
                feature_name=candidate.feature_name,
                feature_family=candidate.feature_family,
                expression_json=encode_json(candidate.expression),
                lineage_json=encode_json(candidate.lineage),
                created_at=created_at,
                raw_json=encode_json(candidate.as_payload()),
            )
            session.add(row)
        evaluation_id = stable_phase_3q_id("evaluation", result.run_id, candidate.candidate_id)
        scorecard = evaluation.scorecard_payload(result.run_id, result.training_as_of)
        eval_row = FeatureEvaluation(
            evaluation_id=evaluation_id,
            candidate_id=candidate.candidate_id,
            run_id=result.run_id,
            outcome_name="net_profitable_after_costs",
            cohort_json=encode_json({"data_mode": result.manifest.data_mode}),
            model_family=None,
            evaluation_policy_id=EVALUATION_POLICY_ID,
            baseline_metrics_json=encode_json(
                {"baseline_rate": _decimal(evaluation.baseline_rate)}
            ),
            candidate_metrics_json=encode_json(
                {
                    "candidate_rate": _decimal(evaluation.candidate_rate),
                    "economic_effect": _decimal(evaluation.economic_effect),
                }
            ),
            paired_deltas_json=encode_json({"paired_delta": _decimal(evaluation.paired_delta)}),
            intervals_json=encode_json({"confidence_interval": "NOT_AVAILABLE"}),
            significance_json=encode_json({"q_value": _decimal(evaluation.q_value)}),
            stability_json=encode_json({"stability_score": _decimal(evaluation.stability_score)}),
            evidence_links_json=encode_json(
                [{"type": "scorecard", "id": stable_phase_3q_id("scorecard", evaluation_id)}]
            ),
            status=evaluation.status,
            composite_score=decimal_to_str(evaluation.composite_score),
            reason_codes_json=encode_json(evaluation.reason_codes),
            created_at=created_at,
            raw_json=encode_json(scorecard),
        )
        session.add(eval_row)
        _persist_fold_rows(
            session,
            result=result,
            evaluation_id=evaluation_id,
            candidate_id=candidate.candidate_id,
            fold_results=evaluation.fold_results,
        )
        _persist_segment_rows(
            session,
            result=result,
            evaluation_id=evaluation_id,
            candidate_id=candidate.candidate_id,
            segment_results=evaluation.segment_results,
        )
        _persist_relationship_rows(
            session,
            result=result,
            candidate_id=candidate.candidate_id,
            relationships=evaluation.relationship_notes,
            created_at=created_at,
        )
        _persist_recommendation_row(
            session,
            result=result,
            evaluation_id=evaluation_id,
            candidate_id=candidate.candidate_id,
            action=evaluation.recommendation_action,
            reason_codes=evaluation.reason_codes,
            created_at=created_at,
        )
    session.flush()


def _persist_fold_rows(
    session: Session,
    *,
    result: FeatureDiscoveryResult,
    evaluation_id: str,
    candidate_id: str,
    fold_results: list[dict[str, Any]],
) -> None:
    for fold in fold_results:
        row = FeatureFoldResult(
            evaluation_id=evaluation_id,
            run_id=result.run_id,
            candidate_id=candidate_id,
            fold_id=str(fold["fold_id"]),
            train_start=_parse_optional(fold.get("train_start")),
            train_end=_parse_optional(fold.get("train_end")),
            validation_start=_parse_optional(fold.get("validation_start")),
            validation_end=_parse_optional(fold.get("validation_end")),
            train_sample_size=int(fold.get("train_sample_size") or 0),
            validation_sample_size=int(fold.get("validation_sample_size") or 0),
            metrics_json=encode_json({"paired_delta": fold.get("paired_delta")}),
            raw_json=encode_json(fold),
        )
        session.add(row)


def _persist_segment_rows(
    session: Session,
    *,
    result: FeatureDiscoveryResult,
    evaluation_id: str,
    candidate_id: str,
    segment_results: list[dict[str, Any]],
) -> None:
    for segment in segment_results:
        row = FeatureSegmentResult(
            evaluation_id=evaluation_id,
            run_id=result.run_id,
            candidate_id=candidate_id,
            segment_key=str(segment["segment_key"]),
            segment_value=str(segment["segment_value"]),
            status=str(segment["status"]),
            sample_size=int(segment["sample_size"]),
            metrics_json=encode_json({"outcome_rate": segment.get("outcome_rate")}),
            raw_json=encode_json(segment),
        )
        session.add(row)


def _persist_relationship_rows(
    session: Session,
    *,
    result: FeatureDiscoveryResult,
    candidate_id: str,
    relationships: list[dict[str, Any]],
    created_at: datetime,
) -> None:
    for relationship in relationships:
        related = str(relationship.get("related_candidate_id") or "")
        relationship_id = stable_phase_3q_id("relationship", result.run_id, candidate_id, related)
        row = FeatureRelationship(
            relationship_id=relationship_id,
            run_id=result.run_id,
            candidate_id=candidate_id,
            related_candidate_id=related,
            relationship_type=str(relationship.get("relationship_type") or "UNKNOWN"),
            strength=relationship.get("strength"),
            reason_codes_json=encode_json(relationship.get("reason_codes") or []),
            created_at=created_at,
            raw_json=encode_json(relationship),
        )
        session.add(row)


def _persist_recommendation_row(
    session: Session,
    *,
    result: FeatureDiscoveryResult,
    evaluation_id: str,
    candidate_id: str,
    action: str,
    reason_codes: list[str],
    created_at: datetime,
) -> None:
    recommendation_id = stable_phase_3q_id("recommendation", result.run_id, candidate_id, action)
    experiment_spec = {}
    if action != ACTION_NO_ACTION:
        experiment_spec = {
            "experiment_spec_id": stable_phase_3q_id("experiment_spec", evaluation_id),
            "source_scorecard_ids": [stable_phase_3q_id("scorecard", evaluation_id)],
            "human_approval_reference": "HUMAN_REVIEW_REQUIRED",
            "production_mutation": "FORBIDDEN",
        }
    row = FeatureRecommendation(
        recommendation_id=recommendation_id,
        run_id=result.run_id,
        candidate_id=candidate_id,
        action=action,
        human_review_required=0 if action == ACTION_NO_ACTION else 1,
        human_approval_reference=None,
        status="PROPOSED" if action != ACTION_NO_ACTION else "NO_ACTION",
        reason_codes_json=encode_json(reason_codes),
        experiment_spec_json=encode_json(experiment_spec),
        created_at=created_at,
        raw_json=encode_json(
            {
                "recommendation_id": recommendation_id,
                "action": action,
                "human_review_required": action != ACTION_NO_ACTION,
                "reason_codes": reason_codes,
                "experiment_spec": experiment_spec,
            }
        ),
    )
    session.add(row)


def _decimal(value) -> str | None:
    return decimal_to_str(value) if value is not None else None


def _parse_optional(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))
