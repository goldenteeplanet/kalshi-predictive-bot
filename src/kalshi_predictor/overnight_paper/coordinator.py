"""One guarded local admission path, with durable decision checkpoints.

Public collection runs outside this local writer. This module takes original,
prepared data records; it never accepts a transport, gateway or execution callback.
SQLite serializes ledger writes, and the activation adapter rechecks capacity in
its own atomic transaction after the immutable shadow is committed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import PaperFill, PaperOrder
from kalshi_predictor.overnight_paper.activation import (
    ExactReleaseEvidence,
    activate_local_paper,
)
from kalshi_predictor.overnight_paper.boundary import LocalPaperAuthorization
from kalshi_predictor.overnight_paper.dataset_store import persist_dataset_record
from kalshi_predictor.overnight_paper.monitoring import MonitoringPermit
from kalshi_predictor.overnight_paper.provenance import Artifact
from kalshi_predictor.overnight_paper.qualification import Readiness, qualify_candidate
from kalshi_predictor.overnight_paper.research_record import PREFIX, research_record
from kalshi_predictor.overnight_paper.store import digest, encode, record_shadow
from kalshi_predictor.paper.models import PaperDecision


@dataclass(frozen=True)
class PreparedCandidate:
    decision: PaperDecision
    qualification_args: dict[str, Any]
    shadow_payload: dict[str, Any]
    evaluation_observation: Artifact | None = None


@dataclass(frozen=True)
class CoordinatorResult:
    state: str
    decision_id: str
    shadow_id: str | None = None
    order_id: int | None = None
    fill_created: bool = False
    blockers: tuple[str, ...] = ()


def assert_public_only_settings(settings: Settings) -> None:
    """Reject unused account material before settings enter immutable journals."""
    if type(settings) is not Settings:
        raise ValueError("CONCRETE_SETTINGS_REQUIRED")
    if any(
        getattr(settings, name) not in (None, "")
        for name in (
            "kalshi_api_key_id",
            "kalshi_private_key_path",
            "postgres_password",
            "execution_confirmation_token",
        )
    ):
        raise ValueError("ACCOUNT_CONFIGURATION_FORBIDDEN_IN_PAPER_EVIDENCE")


def _owned_session_factory(supplied: sessionmaker[Session], path: Path) -> sessionmaker[Session]:
    """Use a concrete local engine with our own session class and options.

    This is a trusted-process boundary, not a sandbox for arbitrary Python hooks.
    In particular, never invoke a caller-provided callable or Session subclass.
    """
    if type(supplied) is not sessionmaker:
        raise ValueError("CONCRETE_SQLITE_SESSION_FACTORY_REQUIRED")
    engine = supplied.kw.get("bind")
    if type(engine) is not Engine or engine.url.get_backend_name() != "sqlite":
        raise ValueError("CONCRETE_SQLITE_ENGINE_REQUIRED")
    if engine.url.get_driver_name() != "pysqlite" or engine.url.query:
        raise ValueError("PLAIN_SQLITE_DATABASE_REQUIRED")
    if not engine.url.database or Path(engine.url.database).resolve() != path:
        raise ValueError("DATABASE_PATH_MISMATCH")
    # Never reuse caller-installed connection creators, pool events or session
    # hooks. The fixed local creator cannot create a new database on a typo/race.
    owned = create_engine(
        f"sqlite:///{path.as_posix()}",
        creator=lambda: sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0),
        poolclass=NullPool,
    )
    return sessionmaker(bind=owned, class_=Session)


def _verify_database(session: Session, path: Path) -> None:
    rows = session.execute(text("PRAGMA database_list")).all()
    if any(row[1] not in {"main", "temp"} for row in rows):
        raise ValueError("ATTACHED_DATABASE_REFUSED")
    main = [row for row in rows if row[1] == "main"]
    if len(main) != 1 or Path(main[0][2]).resolve() != path:
        raise ValueError("DATABASE_PATH_MISMATCH")
    if session.execute(text("PRAGMA quick_check")).scalar_one() != "ok":
        raise ValueError("DATABASE_INTEGRITY_FAILED")


def _checkpoint(session: Session, key: str, at: datetime, payload: dict[str, Any]) -> None:
    encoded = encode(payload)
    existing = session.execute(
        text("SELECT payload FROM overnight_sprint_cycles WHERE id=:key"), {"key": key}
    ).scalar_one_or_none()
    if existing is not None:
        if existing != encoded:
            raise ValueError("IMMUTABLE_CHECKPOINT_CONFLICT")
        return
    session.execute(
        text(
            "INSERT INTO overnight_sprint_cycles(id,captured_at,payload) VALUES(:key,:at,:payload)"
        ),
        {"key": key, "at": at.isoformat(), "payload": encoded},
    )


def _existing_order(
    session: Session, candidate: PreparedCandidate, shadow_id: str
) -> CoordinatorResult | None:
    row = session.execute(
        text("SELECT payload,paper_order_id FROM overnight_shadow WHERE id=:id"), {"id": shadow_id}
    ).first()
    if row is None:
        return None
    if row[0] != encode(candidate.shadow_payload):
        raise ValueError("IMMUTABLE_SHADOW_CONFLICT")
    if row[1] is None:
        return None
    order = session.get(PaperOrder, row[1])
    decision = candidate.decision
    if order is None or (
        order.ticker != decision.ticker
        or order.forecast_id != decision.forecast_id
        or order.side != decision.side
        or order.quantity != 1
        or Decimal(order.limit_price) != decision.limit_price
    ):
        raise ValueError("EXISTING_ORDER_RECONCILIATION_FAILED")
    fills = session.scalars(select(PaperFill).where(PaperFill.paper_order_id == order.id)).all()
    if len(fills) > 1:
        raise ValueError("EXISTING_FILL_RECONCILIATION_FAILED")
    fill = fills[0] if fills else None
    if fill is not None and (
        fill.quantity != 1
        or fill.ticker != decision.ticker
        or fill.side != decision.side
        or Decimal(fill.price) != decision.limit_price
        or not Decimal(fill.fee).is_finite()
        or Decimal(fill.fee) < 0
    ):
        raise ValueError("EXISTING_FILL_RECONCILIATION_FAILED")
    from kalshi_predictor.paper.fees import CONTRACT_KEY, historical_fee_quote

    contract = candidate.shadow_payload.get("qualification_inputs", {}).get(CONTRACT_KEY)
    order_contract = json.loads(order.raw_decision_json).get(CONTRACT_KEY)
    if contract != order_contract or decision.raw_decision_json.get(CONTRACT_KEY) != contract:
        raise ValueError("EXISTING_ORDER_FEE_LINEAGE_MISMATCH")
    if contract is not None:
        quote = historical_fee_quote(
            contract,
            ticker=order.ticker,
            side=order.side,
            quantity=order.quantity,
            price=Decimal(order.limit_price),
        )
        if fill is not None:
            raw = json.loads(fill.raw_fill_json)
            if (
                Decimal(fill.fee) != quote.charge
                or raw.get("fee_contract") != contract
                or raw.get("fee_quote_sha256") != quote.sha256
                or raw.get("fee_provenance") != "GUARDED_FEE_EVIDENCE_V1"
            ):
                raise ValueError("EXISTING_FILL_FEE_LINEAGE_MISMATCH")
    return CoordinatorResult(
        "EXISTING_EXPERIMENT",
        candidate.qualification_args["decision_id"],
        shadow_id,
        order.id,
        fill is not None,
    )


def admit_prepared_candidate(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    candidate: PreparedCandidate,
    authorization: LocalPaperAuthorization,
    objective_bytes: bytes,
    release: ExactReleaseEvidence,
    settings: Settings,
    now: datetime,
    entries_enabled: bool = False,
    monitoring_permit: MonitoringPermit | None = None,
) -> CoordinatorResult:
    """Persist qualification/shadow, then invoke the existing guarded adapter.

    An already-linked experiment is reconciled before any new qualification;
    stale new-entry inputs cannot create a second trade after a restart. Entry
    refusal never disables the separately runnable public settlement watcher.
    """
    assert_public_only_settings(settings)
    if authorization.max_new_positions != 1 or authorization.max_contracts_per_position != 1:
        raise ValueError("ONE_EXPERIMENT_AUTHORIZATION_REQUIRED")
    if settings.execution_enabled or settings.execution_gateway_mode != "disabled":
        raise ValueError("EXCHANGE_EXECUTION_DISABLED_REQUIRED")
    if (
        settings.autopilot_enabled
        or not settings.autopilot_dry_run
        or not settings.execution_dry_run
        or not settings.execution_kill_switch
    ):
        raise ValueError("EXCHANGE_EXECUTION_DISABLED_REQUIRED")
    path = database_path.resolve(strict=True)
    if "onedrive" in str(path).lower():
        raise ValueError("ISOLATED_DATABASE_REQUIRED")
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)()
        for p in (database_path, *database_path.parents)
    ):
        raise ValueError("LINKED_DATABASE_REFUSED")
    session_factory = _owned_session_factory(session_factory, path)
    args = candidate.qualification_args
    inputs = args["decision_inputs"]
    if candidate.shadow_payload.get("qualification_inputs") != inputs:
        raise ValueError("EXACT_SHADOW_INPUTS_REQUIRED")
    shadow_id = digest(candidate.shadow_payload)
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        _verify_database(session, path)
        existing = _existing_order(session, candidate, shadow_id)
        if existing is not None:
            recovered = CoordinatorResult(
                "PAPER_FILLED" if existing.fill_created else "PAPER_ORDER_UNFILLED",
                existing.decision_id,
                shadow_id,
                existing.order_id,
                existing.fill_created,
            )
            _checkpoint(
                session,
                "release-entry:" + args["decision_id"],
                now,
                {"kind": "PAPER_RELEASE_ENTRY", "result": asdict(recovered)},
            )
            session.commit()
            return existing
        total = session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one()
        if total:
            session.rollback()
            return CoordinatorResult(
                "EXPERIMENT_CAPACITY_EXHAUSTED",
                args["decision_id"],
                blockers=("ONE_NEW_PAPER_EXPERIMENT_MAXIMUM",),
            )
        observation = candidate.evaluation_observation
        if observation is not None:
            row = observation.decode()
            if (
                row.get("kind") != "observation-v1"
                or row.get("decision_id") != args["decision_id"]
                or row.get("decision") != inputs
            ):
                raise ValueError("DATASET_DECISION_BINDING_MISMATCH")
            persist_dataset_record(
                session, dataset="paper-release", record=observation, recorded_at=now
            )
        qualification = qualify_candidate(**args)
        qualification_payload = asdict(qualification)
        qualification_payload["net_ev"] = (
            None if qualification.net_ev is None else str(qualification.net_ev)
        )
        qualification_checkpoint = {
            "kind": "PAPER_RELEASE_QUALIFICATION",
            "decision_inputs": inputs,
            "qualification": qualification_payload,
            "shadow_payload": candidate.shadow_payload,
        }
        _checkpoint(
            session,
            "release-qualification:" + args["decision_id"],
            now,
            qualification_checkpoint,
        )
        _checkpoint(
            session,
            PREFIX + args["decision_id"],
            now,
            research_record(qualification_checkpoint),
        )
        if qualification.status != Readiness.PAPER_ELIGIBLE:
            session.commit()
            return CoordinatorResult(
                "BLOCKED", args["decision_id"], blockers=qualification.blockers
            )
        # Reuse the existing shadow writer on this exact transaction/connection.
        db = session.connection().connection.driver_connection
        if type(db) is not sqlite3.Connection:
            raise ValueError("CONCRETE_SQLITE_CONNECTION_REQUIRED")
        if record_shadow(db, candidate.shadow_payload) != shadow_id:
            raise ValueError("SHADOW_ID_MISMATCH")
        _checkpoint(
            session,
            "release-shadow:" + args["decision_id"],
            now,
            {
                "kind": "PAPER_RELEASE_SHADOW",
                "shadow_id": shadow_id,
                "decision_id": args["decision_id"],
            },
        )
        session.commit()
    if not entries_enabled:
        return CoordinatorResult("SHADOW_ONLY", args["decision_id"], shadow_id)
    activated = activate_local_paper(
        session_factory=session_factory,
        database_path=path,
        authorization=authorization,
        objective_bytes=objective_bytes,
        release=release,
        qualification_args=args,
        shadow_payload=candidate.shadow_payload,
        shadow_id=shadow_id,
        decision=candidate.decision,
        settings=settings,
        now=now,
        monitoring_permit=monitoring_permit,
    )
    result = CoordinatorResult(
        "PAPER_FILLED" if activated.fill_created else "PAPER_ORDER_UNFILLED",
        args["decision_id"],
        shadow_id,
        activated.paper_order_id,
        activated.fill_created,
    )
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        _verify_database(session, path)
        _checkpoint(
            session,
            "release-entry:" + args["decision_id"],
            now,
            {
                "kind": "PAPER_RELEASE_ENTRY",
                "result": asdict(result),
            },
        )
        session.commit()
    return result
