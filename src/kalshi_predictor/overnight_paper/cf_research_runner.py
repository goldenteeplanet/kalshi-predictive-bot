"""Current original-bound CF research intake; no collection, inference or orders.

The producer supplies live preparation objects. This entry point never restores
historical engine objects or trusts a caller's clock to make them current.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.config import Settings
from kalshi_predictor.overnight_paper.activation import ExactReleaseEvidence
from kalshi_predictor.overnight_paper.boundary import ExecutionMode, LocalPaperAuthorization
from kalshi_predictor.overnight_paper.cf_candidate_assembly import assemble_cf_research_candidate
from kalshi_predictor.overnight_paper.coordinator import (
    CoordinatorResult,
    admit_prepared_candidate,
    assert_public_only_settings,
)
from kalshi_predictor.overnight_paper.provenance import Artifact
from kalshi_predictor.overnight_paper.provenance_gate import ProvenanceContext
from kalshi_predictor.overnight_paper.rule_verifier import RuleDocument
from kalshi_predictor.overnight_paper.runtime_owner import (
    RuntimeOwner,
    validate_runtime_owner,
)
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.paper.models import PaperDecision
from kalshi_predictor.utils.time import utc_now


def run_cf_research_cycle(
    *, session_factory: sessionmaker[Session], database_path: Path,
    paper_decision: PaperDecision, provenance_args: dict[str, Any],
    authorization: LocalPaperAuthorization, objective_bytes: bytes,
    release: ExactReleaseEvidence, settings: Settings,
    rule_documents: tuple[RuleDocument, ...] = (),
    cost_record: dict[str, Any] | None = None,
    evaluation_observation: Artifact | None = None,
    runtime_owner: RuntimeOwner,
) -> CoordinatorResult:
    """Persist one current qualification/research attempt with entries disabled.

    The outer orchestrator acquires ownership before this guarded entry point.
    Source/provenance/cost checks require that same-process lock to remain active.
    Repeated identical attempts reuse coordinator checkpoints. Stale attempts
    cannot be made current by a historical ``provenance_args['now']`` value.
    A fully qualifying result may produce a shadow, never an order from here.
    """
    assert_public_only_settings(settings)
    if (
        settings.execution_enabled or not settings.execution_dry_run
        or not settings.execution_kill_switch or settings.execution_gateway_mode != "disabled"
        or settings.autopilot_enabled or not settings.autopilot_dry_run
        or settings.paper_order_creation_enabled or not settings.paper_order_kill_switch
    ):
        raise ValueError("CF_RESEARCH_ORDERS_DISABLED_REQUIRED")
    now = utc_now()
    path = database_path.resolve(strict=True)
    if (
        type(authorization) is not LocalPaperAuthorization
        or authorization.mode != ExecutionMode.LOCAL_PAPER
        or authorization.max_new_positions != 1
        or authorization.max_contracts_per_position != 1
        or not 0 < len(objective_bytes) <= 1_000_000
        or hashlib.sha256(objective_bytes).hexdigest() != authorization.objective_sha256
        or authorization.isolated_database_path is None
        or Path(authorization.isolated_database_path).resolve(strict=True) != path
        or not aware(authorization.created_at) <= now < aware(authorization.expires_at)
    ):
        raise ValueError("CF_RESEARCH_CURRENT_OBJECTIVE_AND_DATABASE_REQUIRED")
    context = provenance_args.get("context")
    if type(context) is not ProvenanceContext:
        raise ValueError("CF_RESEARCH_CONCRETE_PROVENANCE_REQUIRED")
    originals = (
        *context.artifacts.values(), *context.source_artifacts,
        *context.training_artifacts, context.features_artifact,
    )
    if (len(originals) > 200 or sum(len(a.payload) for a in originals)
            + len(context.model_code) + sum(len(d.payload) for d in rule_documents) > 20_000_000):
        raise ValueError("CF_RESEARCH_BOUNDED_ORIGINALS_REQUIRED")
    if evaluation_observation is not None and len(evaluation_observation.payload) > 80_000_000:
        raise ValueError("CF_RESEARCH_BOUNDED_OBSERVATION_REQUIRED")
    owner = runtime_owner
    validate_runtime_owner(owner, database_path)
    current = utc_now()
    if not 0 <= (
        current - aware(provenance_args["decision"]["decision_at"])
    ).total_seconds() <= 60:
        raise ValueError("CF_RESEARCH_CURRENT_DECISION_REQUIRED")
    candidate = assemble_cf_research_candidate(
        paper_decision=paper_decision, provenance_args=provenance_args | {"now": current},
        rule_documents=rule_documents, cost_record=cost_record,
        evaluation_observation=evaluation_observation,
    )
    validate_runtime_owner(owner, database_path)
    # Recheck visibility immediately before entering the writer; assembly
    # does not freeze a previously passing freshness check for later use.
    current = utc_now()
    candidate = assemble_cf_research_candidate(
        paper_decision=paper_decision, provenance_args=provenance_args | {"now": current},
        rule_documents=rule_documents, cost_record=candidate.shadow_payload["cost_record"],
        evaluation_observation=evaluation_observation,
    )
    validate_runtime_owner(owner, database_path)
    current = utc_now()
    if not 0 <= (
        current - aware(provenance_args["decision"]["decision_at"])
    ).total_seconds() <= 60:
        raise ValueError("CF_RESEARCH_CURRENT_DECISION_REQUIRED")
    if not aware(authorization.created_at) <= current < aware(authorization.expires_at):
        raise ValueError("CF_RESEARCH_AUTHORIZATION_EXPIRED")
    return admit_prepared_candidate(
        session_factory=session_factory, database_path=database_path, candidate=candidate,
        authorization=authorization, objective_bytes=objective_bytes, release=release,
        settings=settings, now=current, entries_enabled=False,
    )
