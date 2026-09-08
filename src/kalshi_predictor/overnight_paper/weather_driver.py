"""One actual public weather preparation followed by the existing serial supervisor."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.config import Settings
from kalshi_predictor.overnight_paper.acquisition import collect_weather_preparation
from kalshi_predictor.overnight_paper.activation import ExactReleaseEvidence
from kalshi_predictor.overnight_paper.boundary import LocalPaperAuthorization
from kalshi_predictor.overnight_paper.candidate_assembly import assemble_weather_candidate
from kalshi_predictor.overnight_paper.coordinator import (
    _checkpoint,
    _owned_session_factory,
    _verify_database,
    assert_public_only_settings,
)
from kalshi_predictor.overnight_paper.preparation_runner import run_weather_preparation_live_cycle
from kalshi_predictor.overnight_paper.provenance import Artifact
from kalshi_predictor.overnight_paper.rule_verifier import RuleDocument
from kalshi_predictor.overnight_paper.runtime_owner import (
    acquire_runtime_owner,
    validate_runtime_owner,
)
from kalshi_predictor.overnight_paper.supervisor import SupervisorReport, run_paper_supervisor


@dataclass(frozen=True)
class WeatherDriverReport:
    state: str
    generation: str
    preparation_state: str
    assembly_blockers: tuple[str, ...]
    preparation_checkpoint: str | None
    driver_checkpoint: str
    supervisor: SupervisorReport


def run_weather_driver(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    archive_root: Path,
    ticker: str,
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
) -> WeatherDriverReport:
    """No reconstructed engines or supplied PASS; one owner spans every write.

    Current missing model/rule originals yield exact durable rejections. Public
    collection errors also preserve partial captured originals and still run the
    existing settlement supervisor. Database/ownership failures propagate.
    """
    assert_public_only_settings(settings)
    if (
        settings.execution_enabled
        or not settings.execution_dry_run
        or not settings.execution_kill_switch
        or settings.execution_gateway_mode != "disabled"
        or settings.autopilot_enabled
        or not settings.autopilot_dry_run
    ):
        raise ValueError("WEATHER_DRIVER_LOCAL_ONLY_REQUIRED")
    if type(monitoring_cycles) is not int or not 1 <= monitoring_cycles <= 60:
        raise ValueError("WEATHER_DRIVER_MONITORING_BUDGET_REQUIRED")
    if not re.fullmatch(r"[0-9a-f]{40}", code_sha) or (
        release is not None and release.sha != code_sha
    ):
        raise ValueError("WEATHER_DRIVER_RELEASE_SHA_MISMATCH")
    if entries_enabled and type(release) is not ExactReleaseEvidence:
        raise ValueError("WEATHER_DRIVER_ENTRY_RELEASE_EVIDENCE_REQUIRED")
    if archive_root.exists():
        raise ValueError("NEW_PUBLIC_ARCHIVE_REQUIRED")
    path = database_path.resolve(strict=True)
    with acquire_runtime_owner(database_path) as owner:
        factory = _owned_session_factory(session_factory, path)
        with factory() as session:
            _verify_database(session, path)
            baseline_raw = session.execute(
                text("SELECT payload FROM overnight_sprint_cycles WHERE id=:key"),
                {"key": "authorization-baseline:" + str(authorization.database_id)},
            ).scalar_one_or_none()
            if baseline_raw is None:
                raise ValueError("WEATHER_DRIVER_AUTHORIZATION_BASELINE_REQUIRED")
            baseline = json.loads(baseline_raw)
            if (
                Path(baseline["database_path"]).resolve() != path
                or baseline.get("database_id") != authorization.database_id
                or Path(authorization.isolated_database_path or "").resolve() != path
            ):
                raise ValueError("WEATHER_DRIVER_DATABASE_IDENTITY_MISMATCH")
        candidate = None
        preparation_state = "ACQUISITION_NOT_COMPLETED"
        preparation_checkpoint = None
        blockers: tuple[str, ...] = ()
        original_sources: list[dict[str, Any]] = []
        try:
            sources = collect_weather_preparation(archive_root, ticker=ticker)
            original_sources = [
                dict(artifact=s.artifact, sha256=s.sha256, original_utf8=s.payload.decode("utf-8"))
                for s in sources
            ]
            validate_runtime_owner(owner, path)
            cycle = run_weather_preparation_live_cycle(
                session_factory=factory,
                database_path=path,
                cycle_id=owner.generation,
                ticker=ticker,
                source_envelopes=sources,
                settings=settings,
                slippage_allowance=settings.advanced_risk_estimated_slippage_per_contract,
                uncertainty_buffer=settings.advanced_risk_gap_tail_buffer_per_contract,
                runtime_owner=owner,
            )
            preparation_checkpoint = "weather-preparation:" + owner.generation
            preparation_state = cycle.record["state"]
            if cycle.live_result is None:
                blockers = ("HISTORICAL_PREPARATION_REPLAY_NOT_CURRENT",)
            elif cycle.live_result.state != "COMPUTED_UNQUALIFIED":
                blockers = cycle.live_result.blockers or ("WEATHER_PREPARATION_NOT_COMPUTED",)
            else:
                candidate = assemble_weather_candidate(
                    preparation=cycle.live_result,
                    model=model,
                    model_code=model_code,
                    settings=settings,
                    repository=repository,
                    code_sha=code_sha,
                    authorization=authorization,
                    rule_documents=rule_documents,
                    now=datetime.now(UTC),
                    model_evaluation_head_sha256=model_evaluation_head_sha256,
                )
        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
            if any(
                word in str(exc)
                for word in ("DATABASE", "SQLITE", "INPUT_CONFLICT", "INTEGRITY", "RUNTIME_OWNER")
            ):
                raise
            blockers = (str(exc) or type(exc).__name__,)
            # Only generated diagnostic data is read; it is not an admission input.
            partial = archive_root / "source_bundle.json"
            if not original_sources and partial.is_file():
                original_sources = json.loads(partial.read_text())
        if candidate is not None and type(release) is not ExactReleaseEvidence:
            blockers = ("RELEASE_EVIDENCE_REQUIRED",)
            candidate = None
        driver_checkpoint = "weather-driver:" + owner.generation
        validate_runtime_owner(owner, path)
        with factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            _verify_database(session, path)
            _checkpoint(
                session,
                driver_checkpoint,
                datetime.now(UTC),
                {
                    "kind": "PAPER_WEATHER_DRIVER_V1",
                    "generation": owner.generation,
                    "database_id": authorization.database_id,
                    "database_path": str(path),
                    "ticker": ticker,
                    "code_sha": code_sha,
                    "preparation_state": preparation_state,
                    "preparation_checkpoint": preparation_checkpoint,
                    "assembly_blockers": list(blockers),
                    "original_sources": original_sources,
                    "candidate_decision_id": None
                    if candidate is None
                    else candidate.qualification_args["decision_id"],
                    "orders_created": 0,
                },
            )
            session.commit()
        supervised = run_paper_supervisor(
            session_factory=factory,
            database_path=path,
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
        return WeatherDriverReport(
            "STOPPED",
            owner.generation,
            preparation_state,
            blockers,
            preparation_checkpoint,
            driver_checkpoint,
            supervised,
        )
