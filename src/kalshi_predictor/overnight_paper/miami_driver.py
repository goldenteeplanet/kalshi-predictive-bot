"""Captured-original Miami preparation, same-file assembly and existing supervisor.

No acquisition or source clock refresh occurs here. Entries are disabled by
 default; supervisor admission and official settlement checks remain unchanged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.config import Settings
from kalshi_predictor.utils.time import utc_now

from .activation import ExactReleaseEvidence
from .boundary import LocalPaperAuthorization
from .candidate_assembly import assemble_miami_candidate
from .coordinator import _checkpoint
from .miami_binding import MiamiOriginal
from .miami_preparation import assert_miami_settings
from .miami_preparation_runner import run_miami_preparation_live_cycle
from .miami_source_gate import MiamiGateContext
from .miami_storage import owned_miami_factory, verify_miami_storage
from .provenance import Artifact
from .rule_verifier import RuleDocument
from .runtime_owner import acquire_runtime_owner, validate_runtime_owner
from .supervisor import SupervisorReport, run_paper_supervisor


@dataclass(frozen=True)
class MiamiDriverReport:
    state: str
    generation: str
    preparation_state: str
    assembly_blockers: tuple[str, ...]
    preparation_checkpoint: str
    driver_checkpoint: str
    supervisor: SupervisorReport


def run_miami_driver(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    context: MiamiGateContext,
    orderbook: MiamiOriginal,
    orderbook_receipt: Artifact,
    settings: Settings,
    repository: Path,
    code_sha: str,
    authorization: LocalPaperAuthorization,
    objective_bytes: bytes,
    release: ExactReleaseEvidence | None = None,
    model: Artifact | None = None,
    model_code: bytes = b"",
    rule_documents: tuple[RuleDocument, ...] = (),
    entries_enabled: bool = False,
    monitoring_cycles: int = 1,
    model_evaluation_head_sha256: str | None = None,
    fee_evidence: dict[str, Any] | None = None,
) -> MiamiDriverReport:
    """Preserve real IDs in one authorized file; no reconstruction or model promotion."""
    assert_miami_settings(settings)
    if not settings.autopilot_dry_run:
        raise ValueError("MIAMI_DRIVER_LOCAL_ONLY_REQUIRED")
    if type(entries_enabled) is not bool:
        raise ValueError("MIAMI_DRIVER_BOOLEAN_ENTRY_CONTROL_REQUIRED")
    if type(monitoring_cycles) is not int or not 1 <= monitoring_cycles <= 60:
        raise ValueError("MIAMI_DRIVER_MONITORING_BUDGET_REQUIRED")
    if not re.fullmatch(r"[0-9a-f]{40}", code_sha) or (
        release is not None and release.sha != code_sha
    ):
        raise ValueError("MIAMI_DRIVER_RELEASE_SHA_MISMATCH")
    if entries_enabled and type(release) is not ExactReleaseEvidence:
        raise ValueError("MIAMI_DRIVER_ENTRY_RELEASE_EVIDENCE_REQUIRED")
    if hashlib.sha256(objective_bytes).hexdigest() != authorization.objective_sha256:
        raise ValueError("MIAMI_DRIVER_OBJECTIVE_MISMATCH")
    with acquire_runtime_owner(database_path) as owner:
        factory, storage = owned_miami_factory(
            session_factory, database_path=database_path, owner=owner, authorization=authorization
        )
        with factory() as session:
            verify_miami_storage(session, storage, now=utc_now())
        cycle = run_miami_preparation_live_cycle(
            session_factory=factory,
            database_path=storage.database_path,
            runtime_owner=owner,
            authorization=authorization,
            cycle_id=owner.generation,
            context=context,
            orderbook=orderbook,
            orderbook_receipt=orderbook_receipt,
            settings=settings,
            slippage_allowance=settings.advanced_risk_estimated_slippage_per_contract,
            uncertainty_buffer=settings.advanced_risk_gap_tail_buffer_per_contract,
            fee_evidence=fee_evidence,
        )
        candidate = None
        blockers: tuple[str, ...] = ()
        result = cycle.live_result
        if result is None:
            blockers = ("HISTORICAL_PREPARATION_REPLAY_NOT_CURRENT",)
        elif result.state != "COMPUTED_UNQUALIFIED":
            blockers = result.blockers or ("MIAMI_PREPARATION_NOT_COMPUTED",)
        else:
            assert result.owned_storage is not None
            # A new plain session reads the exact same file via the very engine
            # issued for this live preparation; no record is copied or rekeyed.
            with Session(result.owned_storage.engine) as session:
                try:
                    candidate = assemble_miami_candidate(
                        session=session,
                        preparation=result,
                        model=model,
                        model_code=model_code,
                        settings=settings,
                        repository=repository,
                        code_sha=code_sha,
                        authorization=authorization,
                        rule_documents=rule_documents,
                        now=utc_now(),
                        model_evaluation_head_sha256=model_evaluation_head_sha256,
                    )
                except (ValueError, RuntimeError) as exc:
                    if any(
                        word in str(exc)
                        for word in ("DATABASE", "SQLITE", "INTEGRITY", "RUNTIME_OWNER", "STORAGE")
                    ):
                        raise
                    blockers = (str(exc) or type(exc).__name__,)
        if candidate is not None and type(release) is not ExactReleaseEvidence:
            candidate, blockers = None, ("RELEASE_EVIDENCE_REQUIRED",)
        checkpoint = "miami-driver:" + owner.generation
        with factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            verify_miami_storage(session, storage, now=utc_now())
            _checkpoint(
                session,
                checkpoint,
                utc_now(),
                dict(
                    kind="MIAMI_CAPTURED_DRIVER_V1",
                    generation=owner.generation,
                    database_id=authorization.database_id,
                    database_path=str(storage.database_path),
                    code_sha=code_sha,
                    context_sha256=context.fingerprint(),
                    preparation_checkpoint="miami-preparation:" + owner.generation,
                    preparation_state=cycle.record["state"],
                    assembly_blockers=list(blockers),
                    candidate_decision_id=None
                    if candidate is None
                    else candidate.qualification_args["decision_id"],
                    orders_created=0,
                ),
            )
            validate_runtime_owner(owner, storage.database_path)
            session.commit()
        supervised = run_paper_supervisor(
            session_factory=factory,
            database_path=storage.database_path,
            settings=settings,
            code_sha=code_sha,
            candidate=candidate,
            authorization=authorization,
            objective_bytes=objective_bytes,
            release=release,
            entries_enabled=entries_enabled and candidate is not None,
            cycles=monitoring_cycles,
            runtime_owner=owner,
        )
        return MiamiDriverReport(
            "STOPPED",
            owner.generation,
            cycle.record["state"],
            blockers,
            "miami-preparation:" + owner.generation,
            checkpoint,
            supervised,
        )
