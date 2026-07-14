from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.feature_discovery.contracts import (
    MODE_DISABLED,
    RUN_ON_DEMAND,
    RUN_TYPES,
    FeatureDiscoveryConfig,
    FeatureDiscoveryResult,
    checksum_payload,
    stable_phase_3q_id,
)
from kalshi_predictor.feature_discovery.dataset import build_phase3o_discovery_dataset
from kalshi_predictor.feature_discovery.evaluation import evaluate_candidates
from kalshi_predictor.feature_discovery.grammar import generate_candidate_definitions
from kalshi_predictor.feature_discovery.renderer import render_feature_discovery_markdown
from kalshi_predictor.feature_discovery.repository import (
    existing_discovery_run,
    persist_feature_discovery_result,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now


def config_from_settings(settings: Settings | None = None) -> FeatureDiscoveryConfig:
    resolved = settings or get_settings()
    mode = resolved.phase_3q_mode
    if not resolved.phase_3q_feature_discovery_enabled:
        mode = MODE_DISABLED
    config = FeatureDiscoveryConfig(
        operating_mode=mode,
        min_samples=resolved.phase_3q_min_samples,
        max_candidates=resolved.phase_3q_max_candidates,
        min_practical_effect=resolved.phase_3q_min_practical_effect,
        q_value_threshold=resolved.phase_3q_q_value_threshold,
        embargo_seconds=resolved.phase_3q_embargo_seconds,
        purge_seconds=resolved.phase_3q_purge_seconds,
        report_limit=resolved.phase_3q_report_limit,
    )
    config.validate()
    return config


def run_feature_discovery(
    session: Session,
    *,
    run_type: str = RUN_ON_DEMAND,
    training_as_of: datetime | str | None = None,
    output_path: str | Path | None = Path("reports/feature_discovery_report.md"),
    json_output_path: str | Path | None = Path("reports/feature_discovery_report.json"),
    settings: Settings | None = None,
    force: bool = False,
) -> FeatureDiscoveryResult:
    if run_type not in RUN_TYPES:
        raise ValueError(f"Unsupported Phase 3Q run type: {run_type}")
    config = config_from_settings(settings)
    cutoff = parse_datetime(training_as_of) if training_as_of is not None else utc_now()
    if cutoff is None:
        raise ValueError("training_as_of must be a valid datetime.")
    rows, manifest = build_phase3o_discovery_dataset(session, training_as_of=cutoff, config=config)
    source_fields = sorted({field for row in rows for field in row.feature_values})
    candidates = []
    evaluations = []
    status = "COMPLETED"
    if config.operating_mode == MODE_DISABLED:
        status = "DISABLED"
    else:
        candidates = generate_candidate_definitions(source_fields, config=config)
        evaluations = evaluate_candidates(rows, candidates, config=config)
    idempotency_key = _idempotency_key(
        run_type=run_type,
        training_as_of=cutoff,
        manifest_hash=manifest.manifest_hash,
        config=config,
    )
    run_id = stable_phase_3q_id("run", idempotency_key)
    result = FeatureDiscoveryResult(
        run_id=run_id,
        run_type=run_type,
        status=status,
        training_as_of=cutoff,
        manifest=manifest,
        candidate_evaluations=evaluations,
        markdown="",
        report_path=str(output_path) if output_path else None,
        json_path=str(json_output_path) if json_output_path else None,
        idempotent=False,
    )
    markdown = render_feature_discovery_markdown(result)
    result = replace(result, markdown=markdown)
    existing = existing_discovery_run(session, idempotency_key=idempotency_key)
    if existing is not None and not force:
        result = replace(result, idempotent=True)
    else:
        persist_feature_discovery_result(
            session,
            result=result,
            idempotency_key=idempotency_key,
            artifact_uris={
                "markdown_report": str(output_path) if output_path else None,
                "json_report": str(json_output_path) if json_output_path else None,
            },
        )
    _write_outputs(result, output_path=output_path, json_output_path=json_output_path)
    return result


def _idempotency_key(
    *,
    run_type: str,
    training_as_of: datetime,
    manifest_hash: str,
    config: FeatureDiscoveryConfig,
) -> str:
    payload = {
        "run_type": run_type,
        "training_as_of": training_as_of.isoformat(),
        "dataset_spec_hash": manifest_hash,
        "source_manifest_hash": manifest_hash,
        "candidate_policy_version": config.candidate_policy_id,
        "evaluation_policy_version": config.evaluation_policy_id,
        "configuration_version": config.configuration_version,
    }
    return checksum_payload(payload)


def _write_outputs(
    result: FeatureDiscoveryResult,
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
        payload = {
            "run_id": result.run_id,
            "run_type": result.run_type,
            "status": result.status,
            "training_as_of": result.training_as_of.isoformat(),
            "manifest": result.manifest.as_payload(),
            "candidate_counts": result.candidate_counts,
            "scorecards": [
                evaluation.scorecard_payload(result.run_id, result.training_as_of)
                for evaluation in result.candidate_evaluations
            ],
            "idempotent": result.idempotent,
        }
        output.write_text(encode_json(payload), encoding="utf-8")


def phase3p_evidence_references(result: FeatureDiscoveryResult) -> list[dict[str, str]]:
    return [
        {
            "source": "phase_3q",
            "run_id": result.run_id,
            "scorecard_id": stable_phase_3q_id(
                "scorecard",
                result.run_id,
                evaluation.candidate.candidate_id,
            ),
            "status": evaluation.status,
            "candidate_id": evaluation.candidate.candidate_id,
        }
        for evaluation in result.candidate_evaluations
    ]
