"""Atomic actual Miami preparation and historical-only cycle journal in an owned file."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.config import Settings
from kalshi_predictor.utils.time import utc_now

from .boundary import LocalPaperAuthorization, authorization_fingerprint
from .coordinator import _checkpoint
from .miami_binding import MiamiOriginal
from .miami_preparation import (
    MiamiPreparationResult,
    assert_miami_settings,
    prepare_owned_miami_candidate,
    verify_miami_preparation_handoff,
)
from .miami_provenance import _artifact_row, _original_row
from .miami_source_gate import MiamiGateContext
from .miami_storage import owned_miami_factory, verify_miami_storage
from .preparation_runner import _journal_value
from .provenance import Artifact, canonical_hash
from .runtime_owner import RuntimeOwner, validate_runtime_owner


@dataclass(frozen=True)
class MiamiPreparationCycle:
    record: dict[str, Any]
    live_result: MiamiPreparationResult | None


def run_miami_preparation_live_cycle(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    runtime_owner: RuntimeOwner,
    authorization: LocalPaperAuthorization,
    cycle_id: str,
    context: MiamiGateContext,
    orderbook: MiamiOriginal,
    orderbook_receipt: Artifact,
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    fee_evidence: dict[str, Any] | None,
) -> MiamiPreparationCycle:
    """One bounded request, actual same-file records, atomic journal; replay has no live handoff.

    Caller must keep this owner active through assembly/admission. No owner is
    acquired implicitly because a live result cannot outlast its ownership.
    """
    assert_miami_settings(settings)
    if type(cycle_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cycle_id):
        raise ValueError("INVALID_MIAMI_PREPARATION_CYCLE_ID")
    if type(context) is not MiamiGateContext:
        raise ValueError("CONCRETE_MIAMI_CONTEXT_REQUIRED")
    originals = context.artifacts() + (orderbook.artifact, orderbook_receipt)
    if len(originals) > 75 or sum(len(a.payload) for a in originals) > 16_000_000:
        raise ValueError("BOUNDED_MIAMI_ORIGINALS_REQUIRED")
    if any(hashlib.sha256(a.payload).hexdigest() != a.sha256 for a in originals):
        raise ValueError("MIAMI_ORIGINAL_HASH_REQUIRED")
    factory, storage = owned_miami_factory(
        session_factory,
        database_path=database_path,
        owner=runtime_owner,
        authorization=authorization,
    )
    # Preserve exact original bytes and all source-clock associations; hash-only
    # metadata cannot replace an original on cycle replay.
    request = _journal_value(
        dict(
            context_sha256=context.fingerprint(),
            originals=[_artifact_row(a) for a in originals],
            book=_original_row(orderbook),
            book_receipt=_artifact_row(orderbook_receipt),
            settings=settings.model_dump(mode="json"),
            fee_evidence=fee_evidence,
            slippage_allowance=slippage_allowance,
            uncertainty_buffer=uncertainty_buffer,
            database_id=authorization.database_id,
            authorization_sha256=authorization_fingerprint(authorization),
            database_path=str(storage.database_path),
            preparation_source_sha256=hashlib.sha256(
                Path(__file__).with_name("miami_preparation.py").read_bytes()
            ).hexdigest(),
        )
    )
    request_id = canonical_hash(request)
    if len(json.dumps(request)) > 40_000_000:
        raise ValueError("BOUNDED_MIAMI_REQUEST_REQUIRED")
    key = "miami-preparation:" + cycle_id
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        verify_miami_storage(session, storage, now=utc_now())
        stored = session.execute(
            text("SELECT payload FROM overnight_sprint_cycles WHERE id=:id"), dict(id=key)
        ).scalar_one_or_none()
        if stored is not None:
            record = json.loads(stored)
            if (
                record.get("request_id") != request_id
                or canonical_hash(record.get("request")) != request_id
            ):
                raise ValueError("MIAMI_PREPARATION_CYCLE_INPUT_CONFLICT")
            session.rollback()
            return MiamiPreparationCycle(record, None)
        started = utc_now()
        result = prepare_owned_miami_candidate(
            session,
            storage=storage,
            context=context,
            orderbook=orderbook,
            orderbook_receipt=orderbook_receipt,
            settings=settings,
            slippage_allowance=slippage_allowance,
            uncertainty_buffer=uncertainty_buffer,
            fee_evidence=fee_evidence,
        )
        if result.state == "COMPUTED_UNQUALIFIED":
            verify_miami_preparation_handoff(session, result, now=utc_now())
        verify_miami_storage(session, storage, now=utc_now())
        record = _journal_value(
            dict(
                kind="MIAMI_OWNED_PREPARATION_V1",
                request=request,
                request_id=request_id,
                started_at=started,
                finished_at=utc_now(),
                state=result.state,
                blockers=result.blockers,
                records=result.records,
                source_bundle=result.source_bundle,
                current_eligibility=False,
                orders_created=0,
            )
        )
        _checkpoint(session, key, utc_now(), record)
        validate_runtime_owner(runtime_owner, storage.database_path)
        session.commit()
        return MiamiPreparationCycle(record, result)
