"""Serial bounded owner of existing paper admission and settlement components."""

from __future__ import annotations

import re
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.config import Settings
from kalshi_predictor.overnight_paper.activation import ExactReleaseEvidence
from kalshi_predictor.overnight_paper.boundary import LocalPaperAuthorization
from kalshi_predictor.overnight_paper.coordinator import (
    CoordinatorResult,
    PreparedCandidate,
    _checkpoint,
    _owned_session_factory,
    _verify_database,
    admit_prepared_candidate,
    assert_public_only_settings,
)
from kalshi_predictor.overnight_paper.monitoring import monitor_owned_once, revoke_monitoring
from kalshi_predictor.overnight_paper.runtime_owner import (
    RuntimeOwner,
    acquire_runtime_owner,
    validate_runtime_owner,
)


@dataclass(frozen=True)
class SupervisorReport:
    state: str
    generation: str
    cycles_completed: int
    admission_state: str | None
    entries_enabled: bool
    stopped_reason: str
    health_events: tuple[dict[str, Any], ...]


def _now() -> datetime:
    return datetime.now(UTC)


def run_paper_supervisor(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    settings: Settings,
    code_sha: str,
    cycles: int = 1,
    interval_seconds: int = 60,
    candidate: PreparedCandidate | None = None,
    authorization: LocalPaperAuthorization | None = None,
    objective_bytes: bytes | None = None,
    release: ExactReleaseEvidence | None = None,
    entries_enabled: bool = False,
    runtime_owner: RuntimeOwner | None = None,
) -> SupervisorReport:
    """Actual finite foreground execution, never a promise of future monitoring.

    No custom transport, clock, sleeper, writer or execution callback is accepted.
    Public acquisition/forecast preparation occurs separately. The same frozen
    candidate can be attempted once; changed/stale inputs require a later run with
    a newly prepared decision. All health records share the existing ledger.
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
        raise ValueError("SUPERVISOR_LOCAL_ONLY_REQUIRED")
    if type(cycles) is not int or not 1 <= cycles <= 60 or interval_seconds != 60:
        raise ValueError("SUPERVISOR_BOUNDED_CYCLES_REQUIRED")
    if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
        raise ValueError("SUPERVISOR_CODE_SHA_REQUIRED")
    if candidate is not None and (
        type(candidate) is not PreparedCandidate
        or authorization is None
        or objective_bytes is None
        or release is None
        or release.sha != code_sha
    ):
        raise ValueError("SUPERVISOR_CANDIDATE_RELEASE_CONTEXT_REQUIRED")
    path = database_path.resolve(strict=True)
    events: list[dict[str, Any]] = []
    completed = 0
    admission_state = None
    entry_allowed = bool(entries_enabled and candidate is not None)
    attempted = False
    reason = "BOUNDED_RUN_COMPLETE"
    if runtime_owner is not None:
        validate_runtime_owner(runtime_owner, database_path)
    ownership = (
        acquire_runtime_owner(database_path)
        if runtime_owner is None
        else nullcontext(runtime_owner)
    )
    with ownership as owner:
        factory = _owned_session_factory(session_factory, path)

        def health(state: str, **details: Any) -> None:
            validate_runtime_owner(owner, path)
            at = _now()
            payload = {
                "kind": "PAPER_RUNTIME_HEALTH_V1",
                "generation": owner.generation,
                "sequence": len(events),
                "pid": owner.pid,
                "process_identity": owner.process_identity,
                "process_start_identity": getattr(owner, "process_start_identity", None),
                "database_path": str(path),
                "database_file_identity": list(owner.database_file_identity),
                "code_sha": code_sha,
                "state": state,
                "entries_enabled": entry_allowed,
                "captured_at": at.isoformat(),
                **details,
            }
            with factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                _verify_database(session, path)
                baselines = (
                    session.execute(
                        text(
                            "SELECT payload FROM overnight_sprint_cycles "
                            "WHERE id LIKE 'authorization-baseline:%'"
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(baselines) != 1:
                    raise ValueError("SUPERVISOR_SINGLE_BASELINE_REQUIRED")
                import json

                baseline = json.loads(baselines[0])
                if (
                    baseline.get("kind") != "LOCAL_PAPER_AUTHORIZATION_BASELINE_V1"
                    or Path(baseline["database_path"]).resolve() != path
                ):
                    raise ValueError("SUPERVISOR_BASELINE_IDENTITY_REQUIRED")
                payload["database_id"] = baseline["database_id"]
                _checkpoint(
                    session, f"runtime-health:{owner.generation}:{len(events):06d}", at, payload
                )
                session.commit()
            events.append(payload)

        def monitor() -> Any:
            result = monitor_owned_once(owner=owner, session_factory=factory, code_sha=code_sha)
            health(
                "SETTLEMENT_MONITORING",
                watcher_status=result.report.status,
                watcher_cycles_completed=result.report.cycles_completed,
                live_candidate_permits=len(result.permits),
            )
            return result

        def admit_once(**arguments: Any) -> CoordinatorResult:
            nonlocal entry_allowed
            try:
                return admit_prepared_candidate(**arguments)
            except ValueError as exc:
                # Eligibility/release staleness must stop entries, not monitoring.
                # Database, identity-journal or reconciliation failures remain fatal.
                message = str(exc)
                if any(
                    marker in message
                    for marker in (
                        "DATABASE",
                        "SQLITE",
                        "IMMUTABLE",
                        "RECONCILIATION",
                        "INTEGRITY",
                    )
                ):
                    raise
                entry_allowed = False
                revoke_monitoring(owner)
                return CoordinatorResult(
                    "BLOCKED",
                    arguments["candidate"].qualification_args["decision_id"],
                    blockers=(message,),
                )

        try:
            health("RUNNING")
            for index in range(cycles):
                try:
                    monitor()
                except httpx.HTTPError as exc:
                    revoke_monitoring(owner)
                    entry_allowed = False
                    health(
                        "DEGRADED", reason=type(exc).__name__, operation="settlement_public_read"
                    )
                    if index + 1 < cycles:
                        time.sleep(interval_seconds)
                    continue
                completed += 1
                with factory() as session:
                    _verify_database(session, path)
                    existing = session.execute(
                        text("SELECT count(*) FROM paper_orders")
                    ).scalar_one()
                if existing:
                    entry_allowed = False
                    health("ENTRY_DISABLED", reason="EXPERIMENT_ALREADY_EXISTS")
                if candidate is not None and not attempted and not existing:
                    attempted = True
                    assert (
                        authorization is not None
                        and objective_bytes is not None
                        and release is not None
                    )
                    shared: dict[str, Any] = dict(
                        session_factory=factory,
                        database_path=path,
                        candidate=candidate,
                        authorization=authorization,
                        objective_bytes=objective_bytes,
                        release=release,
                        settings=settings,
                    )
                    shadow = admit_once(**shared, now=_now(), entries_enabled=False)
                    admission_state = shadow.state
                    health(
                        "BLOCKED" if shadow.blockers else "FRESH",
                        admission_state=shadow.state,
                        decision_id=shadow.decision_id,
                        blockers=list(shadow.blockers),
                    )
                    if entry_allowed and shadow.state == "SHADOW_ONLY":
                        try:
                            current = monitor()
                        except httpx.HTTPError as exc:
                            revoke_monitoring(owner)
                            entry_allowed = False
                            health(
                                "DEGRADED", reason=type(exc).__name__, operation="candidate_monitor"
                            )
                            current = None
                        matching = (
                            []
                            if current is None
                            else [p for p in current.permits if p.shadow_id == shadow.shadow_id]
                        )
                        if len(matching) != 1:
                            admission_state = "MONITORING_NOT_READY"
                            health("BLOCKED", reason=admission_state)
                        else:
                            admitted = admit_once(
                                **shared,
                                now=_now(),
                                entries_enabled=True,
                                monitoring_permit=matching[0],
                            )
                            admission_state = admitted.state
                            entry_allowed = False
                            health(
                                "ENTRY_DISABLED",
                                admission_state=admitted.state,
                                order_id=admitted.order_id,
                                decision_id=admitted.decision_id,
                                blockers=list(admitted.blockers),
                            )
                    entry_allowed = False
                if not entry_allowed:
                    revoke_monitoring(owner)
                if index + 1 < cycles:
                    time.sleep(interval_seconds)
        except BaseException as exc:
            entry_allowed = False
            reason = type(exc).__name__ + ":" + str(exc)
            # A DB/identity failure may prevent even a health write. Do not hide it
            # behind a fabricated committed STOPPED event or continue other writes.
            raise
        finally:
            revoke_monitoring(owner)
            entry_allowed = False
            if reason == "BOUNDED_RUN_COMPLETE":
                health("STOPPED", reason=reason)
    return SupervisorReport(
        "STOPPED", owner.generation, completed, admission_state, False, reason, tuple(events)
    )
