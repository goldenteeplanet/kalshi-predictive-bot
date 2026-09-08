"""Durable existing-stack weather preparation, separate from guarded admission."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.config import Settings
from kalshi_predictor.overnight_paper import preparation
from kalshi_predictor.overnight_paper.coordinator import (
    _owned_session_factory,
    _verify_database,
    assert_public_only_settings,
)
from kalshi_predictor.overnight_paper.qualification import EvidenceReference
from kalshi_predictor.overnight_paper.runtime_owner import (
    RuntimeOwner,
    acquire_runtime_owner,
    validate_runtime_owner,
)
from kalshi_predictor.overnight_paper.store import digest, encode
from kalshi_predictor.utils.time import utc_now


def _journal_value(value: Any) -> Any:
    """Explicit lossless data conversion; never stringify unknown executable objects."""
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("NONFINITE_JOURNAL_DECIMAL")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("JOURNAL_TIMEZONE_REQUIRED")
        return value.isoformat()
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JOURNAL_STRING_KEYS_REQUIRED")
        return {key: _journal_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_journal_value(item) for item in value]
    raise ValueError("UNSUPPORTED_JOURNAL_VALUE")


@dataclass(frozen=True)
class PreparationCycle:
    record: dict[str, Any]
    live_result: preparation.WeatherPreparationResult | None


def _run_weather_preparation_live_cycle(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    cycle_id: str,
    ticker: str,
    source_envelopes: tuple[EvidenceReference, ...],
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
) -> PreparationCycle:
    """Compute once and preserve rejection or computation on the same ledger.

    Replay returns the original historical computation and does not refresh its
    clocks. A changed request needs a new cycle ID. This never admits a trade.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cycle_id):
        raise ValueError("INVALID_PREPARATION_CYCLE_ID")
    assert_public_only_settings(settings)
    if not 1 <= len(source_envelopes) <= 12 or any(
        len(source.payload) > 1_000_000 or not source.valid() for source in source_envelopes
    ):
        raise ValueError("BOUNDED_ORIGINAL_ENVELOPES_REQUIRED")
    path = database_path.resolve(strict=True)
    if "onedrive" in str(path).lower() or any(
        item.is_symlink() or getattr(item, "is_junction", lambda: False)()
        for item in (database_path, *database_path.parents)
    ):
        raise ValueError("ISOLATED_UNLINKED_DATABASE_REQUIRED")
    factory = _owned_session_factory(session_factory, path)
    request = {
        "ticker": ticker,
        "source_hashes": [source.sha256 for source in source_envelopes],
        "settings": settings.model_dump(mode="json"),
        "slippage_allowance": str(slippage_allowance),
        "uncertainty_buffer": str(uncertainty_buffer),
        "preparation_source_sha256": hashlib.sha256(
            Path(preparation.__file__).read_bytes()
        ).hexdigest(),
    }
    request_id = digest(request)
    key = "weather-preparation:" + cycle_id
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        _verify_database(session, path)
        stored = session.execute(
            text("SELECT payload FROM overnight_sprint_cycles WHERE id=:id"), {"id": key}
        ).scalar_one_or_none()
        if stored is not None:
            record = json.loads(stored)
            if record.get("request_id") != request_id or record.get("request") != request:
                raise ValueError("PREPARATION_CYCLE_INPUT_CONFLICT")
            session.rollback()
            return PreparationCycle(record, None)
        started = utc_now()
        result = preparation.prepare_weather_candidate(
            session,
            ticker=ticker,
            source_envelopes=source_envelopes,
            settings=settings,
            slippage_allowance=slippage_allowance,
            uncertainty_buffer=uncertainty_buffer,
        )
        record = _journal_value(
            {
                "kind": "PAPER_RELEASE_PREPARATION",
                "request_id": request_id,
                "request": request,
                "started_at": started,
                "finished_at": utc_now(),
                "state": result.state,
                "blockers": result.blockers,
                "records": result.records,
                "decision": None if result.decision is None else asdict(result.decision),
                "original_sources": [
                    {
                        "artifact": source.artifact,
                        "sha256": source.sha256,
                        "original_utf8": source.payload.decode("utf-8"),
                    }
                    for source in source_envelopes
                ],
                "orders_created": 0,
                "current_eligibility": False,
            }
        )
        session.execute(
            text("INSERT INTO overnight_sprint_cycles(id,captured_at,payload) VALUES(:id,:at,:p)"),
            {"id": key, "at": record["finished_at"], "p": encode(record)},
        )
        session.commit()
        return PreparationCycle(record, result)


def run_weather_preparation_live_cycle(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    cycle_id: str,
    ticker: str,
    source_envelopes: tuple[EvidenceReference, ...],
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    runtime_owner: RuntimeOwner | None = None,
) -> PreparationCycle:
    """Own the ledger or validate an already-held same-process driver owner."""
    assert_public_only_settings(settings)
    if runtime_owner is not None:
        validate_runtime_owner(runtime_owner, database_path)
    ownership = (
        acquire_runtime_owner(database_path)
        if runtime_owner is None
        else nullcontext(runtime_owner)
    )
    with ownership as owner:
        validate_runtime_owner(owner, database_path)
        return _run_weather_preparation_live_cycle(
            session_factory=session_factory,
            database_path=database_path,
            cycle_id=cycle_id,
            ticker=ticker,
            source_envelopes=source_envelopes,
            settings=settings,
            slippage_allowance=slippage_allowance,
            uncertainty_buffer=uncertainty_buffer,
        )


def run_weather_preparation_cycle(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    cycle_id: str,
    ticker: str,
    source_envelopes: tuple[EvidenceReference, ...],
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    runtime_owner: RuntimeOwner | None = None,
) -> dict[str, Any]:
    """Historical journal API; replay never reconstructs current engine objects."""
    return run_weather_preparation_live_cycle(
        session_factory=session_factory,
        database_path=database_path,
        cycle_id=cycle_id,
        ticker=ticker,
        source_envelopes=source_envelopes,
        settings=settings,
        slippage_allowance=slippage_allowance,
        uncertainty_buffer=uncertainty_buffer,
        runtime_owner=runtime_owner,
    ).record
