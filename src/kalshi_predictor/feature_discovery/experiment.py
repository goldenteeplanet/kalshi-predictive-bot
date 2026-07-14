from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import FeatureEvaluation, FeatureRecommendation
from kalshi_predictor.feature_discovery.contracts import ACTION_NO_ACTION, stable_phase_3q_id
from kalshi_predictor.utils.time import utc_now


def export_feature_experiment_spec(
    session: Session,
    *,
    evaluation_id: str,
    human_approval_reference: str,
    output_path: str | Path,
) -> Path:
    if not human_approval_reference.strip():
        raise ValueError("Feature experiment export requires a human approval reference.")
    evaluation = session.get(FeatureEvaluation, evaluation_id)
    if evaluation is None:
        raise ValueError(f"Unknown feature evaluation: {evaluation_id}")
    if evaluation.status == "REJECTED":
        raise ValueError("Rejected candidates cannot be exported as experiments.")

    recommendation = _recommendation_for(session, evaluation)
    if recommendation is None or recommendation.action == ACTION_NO_ACTION:
        raise ValueError("Candidate has no experiment recommendation to export.")

    spec = {
        "experiment_spec_id": stable_phase_3q_id(
            "experiment_spec",
            evaluation_id,
            human_approval_reference,
        ),
        "source_scorecard_ids": [
            stable_phase_3q_id("scorecard", evaluation.run_id, evaluation.candidate_id)
        ],
        "feature_definition_ids": [evaluation.candidate_id],
        "baseline_feature_set_id": "current_approved_feature_set",
        "target_outcome_spec_id": "net_profitable_after_costs_v1",
        "training_dataset_spec": decode_json(evaluation.cohort_json),
        "validation_policy_id": evaluation.evaluation_policy_id,
        "model_families": ["offline_research_only"],
        "success_metrics": ["paired_delta", "economic_effect_net_pnl_per_contract", "q_value"],
        "failure_metrics": ["holdout_failure", "instability", "data_quality_regression"],
        "sample_requirements": {"minimum_current_sample": "configured"},
        "shadow_requirements": {"paper_only": True, "live_execution": False},
        "rollback_criteria": ["failed_holdout", "unstable_recent_window", "negative_net_effect"],
        "human_approval_reference": human_approval_reference,
        "production_mutation": "FORBIDDEN",
        "created_at": utc_now().isoformat(),
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encode_json(spec), encoding="utf-8")
    recommendation.human_approval_reference = human_approval_reference
    recommendation.experiment_spec_json = encode_json(spec)
    recommendation.raw_json = encode_json(
        {
            "recommendation_id": recommendation.recommendation_id,
            "action": recommendation.action,
            "human_review_required": True,
            "human_approval_reference": human_approval_reference,
            "experiment_spec": spec,
        }
    )
    session.flush()
    return output


def _recommendation_for(
    session: Session,
    evaluation: FeatureEvaluation,
) -> FeatureRecommendation | None:
    return next(
        (
            row
            for row in session.query(FeatureRecommendation)
            .filter(FeatureRecommendation.run_id == evaluation.run_id)
            .filter(FeatureRecommendation.candidate_id == evaluation.candidate_id)
            .all()
            if row.action != ACTION_NO_ACTION
        ),
        None,
    )
