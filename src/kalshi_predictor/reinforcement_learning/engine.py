from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.reinforcement_learning.contracts import (
    FORMULATION,
    MODE_DISABLED,
    STATUS_COMPLETED,
    STATUS_DISABLED,
    RewardDefinition,
    RLConfig,
    RLEvaluationResult,
    checksum_payload,
    stable_phase_3s_id,
)
from kalshi_predictor.reinforcement_learning.dataset import build_rl_dataset
from kalshi_predictor.reinforcement_learning.evaluation import (
    baseline_policy_identity,
    candidate_policy_identity,
    evaluate_policies,
)
from kalshi_predictor.reinforcement_learning.renderer import render_rl_policy_markdown
from kalshi_predictor.reinforcement_learning.repository import (
    existing_rl_run,
    persist_rl_evaluation_result,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now


def config_from_settings(settings: Settings | None = None) -> RLConfig:
    resolved = settings or get_settings()
    mode = resolved.phase_3s_mode
    if not resolved.phase_3s_reinforcement_learning_enabled:
        mode = MODE_DISABLED
    config = RLConfig(
        enabled=resolved.phase_3s_reinforcement_learning_enabled,
        mode=mode,
        min_training_rows=resolved.phase_3s_min_training_rows,
        min_action_support=resolved.phase_3s_min_action_support,
        baseline_opportunity_score=resolved.phase_3s_baseline_opportunity_score,
        candidate_opportunity_score=resolved.phase_3s_candidate_opportunity_score,
        min_lcb_improvement=resolved.phase_3s_min_lcb_improvement,
        allow_online_exploration=resolved.phase_3s_allow_online_exploration,
        governed_gate_enabled=resolved.phase_3s_governed_gate_enabled,
    )
    config.validate()
    return config


def run_rl_evaluation(
    session: Session,
    *,
    run_type: str = "EVALUATE",
    training_as_of: str | Any | None = None,
    output_path: str | Path | None = Path("reports/rl_policy_report.md"),
    json_output_path: str | Path | None = Path("reports/rl_policy_report.json"),
    settings: Settings | None = None,
    force: bool = False,
) -> RLEvaluationResult:
    resolved_settings = settings or get_settings()
    config = config_from_settings(resolved_settings)
    created_at = utc_now()
    cutoff = parse_datetime(training_as_of) if training_as_of is not None else created_at
    if cutoff is None:
        raise ValueError("training_as_of must be a valid datetime.")
    reward_definition = RewardDefinition()
    dataset = build_rl_dataset(
        session,
        training_as_of=cutoff,
        config=config,
        reward_definition=reward_definition,
    )
    if config.mode == MODE_DISABLED:
        evaluation_payload = {
            "estimator_results": (),
            "economic_metrics": {},
            "risk_metrics": {},
            "behavior_support": {},
            "acceptance_gates": (),
            "recommendation_status": STATUS_DISABLED,
            "reason_codes": ("RL_DISABLED",),
        }
        status = STATUS_DISABLED
    else:
        evaluation_payload = evaluate_policies(dataset, config=config)
        status = STATUS_COMPLETED
    baseline_policy = baseline_policy_identity()
    candidate_policy = candidate_policy_identity(config)
    idempotency_key = _idempotency_key(
        run_type=run_type,
        training_as_of=cutoff,
        dataset_hash=dataset.dataset_hash,
        config=config,
    )
    persist_key = f"{idempotency_key}:force:{created_at.isoformat()}" if force else idempotency_key
    run_id = stable_phase_3s_id("run", persist_key)
    evaluation_id = stable_phase_3s_id("evaluation", run_id, dataset.dataset_manifest_id)
    card = _evaluation_card(
        run_id=run_id,
        evaluation_id=evaluation_id,
        created_at=created_at,
        training_as_of=cutoff,
        dataset=dataset,
        reward_definition=reward_definition,
        candidate_policy=candidate_policy,
        baseline_policy=baseline_policy,
        status=status,
        evaluation_payload=evaluation_payload,
    )
    result = RLEvaluationResult(
        run_id=run_id,
        run_type=run_type,
        mode=config.mode,
        status=status,
        evaluation_id=evaluation_id,
        created_at=created_at,
        training_as_of=cutoff,
        dataset=dataset,
        reward_definition=reward_definition,
        candidate_policy=candidate_policy,
        baseline_policy=baseline_policy,
        estimator_results=tuple(evaluation_payload["estimator_results"]),
        economic_metrics=evaluation_payload["economic_metrics"],
        risk_metrics=evaluation_payload["risk_metrics"],
        behavior_support=evaluation_payload["behavior_support"],
        acceptance_gates=tuple(evaluation_payload["acceptance_gates"]),
        recommendation_status=evaluation_payload["recommendation_status"],
        reason_codes=tuple(evaluation_payload["reason_codes"]),
        card=card,
        markdown="",
        report_path=str(output_path) if output_path else None,
        json_path=str(json_output_path) if json_output_path else None,
        idempotent=False,
    )
    result = replace(result, markdown=render_rl_policy_markdown(result))
    existing = existing_rl_run(session, idempotency_key=idempotency_key)
    if existing is not None and not force:
        result = replace(result, idempotent=True)
    else:
        persist_rl_evaluation_result(
            session,
            result=result,
            idempotency_key=persist_key,
            artifact_uris={
                "markdown_report": str(output_path) if output_path else None,
                "json_report": str(json_output_path) if json_output_path else None,
            },
        )
    _write_outputs(result, output_path=output_path, json_output_path=json_output_path)
    return result


def _evaluation_card(
    *,
    run_id: str,
    evaluation_id: str,
    created_at: Any,
    training_as_of: Any,
    dataset: Any,
    reward_definition: RewardDefinition,
    candidate_policy: Any,
    baseline_policy: Any,
    status: str,
    evaluation_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "evaluation_id": evaluation_id,
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "formulation": FORMULATION,
        "evaluation_status": status,
        "candidate_policy": candidate_policy.as_payload(),
        "baseline_policy": baseline_policy.as_payload(),
        "reward": reward_definition.as_payload(),
        "windows": {"training_as_of": training_as_of.isoformat()},
        "data_coverage": dataset.as_payload(),
        "action_space": ["SKIP", "PROCEED"],
        "behavior_support": evaluation_payload["behavior_support"],
        "estimator_results": [
            estimator.as_payload() for estimator in evaluation_payload["estimator_results"]
        ],
        "economic_metrics": evaluation_payload["economic_metrics"],
        "risk_metrics": evaluation_payload["risk_metrics"],
        "acceptance_gates": list(evaluation_payload["acceptance_gates"]),
        "recommendation": {
            "status": evaluation_payload["recommendation_status"],
            "human_review_required": True,
            "online_exploration_enabled": False,
            "phase_3m_remains_sizing_authority": True,
            "phase_3n_remains_final_risk_authority": True,
            "reason_codes": list(evaluation_payload["reason_codes"]),
        },
        "lineage": {
            "configuration_version": "phase_3s_default_v1",
            "dataset_hash": dataset.dataset_hash,
        },
    }


def _idempotency_key(
    *,
    run_type: str,
    training_as_of: Any,
    dataset_hash: str,
    config: RLConfig,
) -> str:
    return checksum_payload(
        {
            "run_type": run_type,
            "training_as_of": training_as_of.isoformat(),
            "dataset_hash": dataset_hash,
            "mode": config.mode,
            "configuration_version": config.configuration_version,
        }
    )


def _write_outputs(
    result: RLEvaluationResult,
    *,
    output_path: str | Path | None,
    json_output_path: str | Path | None,
) -> None:
    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.markdown, encoding="utf-8")
    if json_output_path is not None:
        output = Path(json_output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encode_json(result.card), encoding="utf-8")
