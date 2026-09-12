"""Durable, explicitly incomplete CF preparation; never a qualified candidate."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.advanced_risk.cf_costs import cf_risk_cost_scope
from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import Forecast, Market
from kalshi_predictor.overnight_paper.boundary import ExecutionMode, LocalPaperAuthorization
from kalshi_predictor.overnight_paper.cf_preparation_record import (
    PREFIX,
    preparation_cost_block_record,
)
from kalshi_predictor.overnight_paper.coordinator import (
    _checkpoint,
    _owned_session_factory,
    _verify_database,
    assert_public_only_settings,
)
from kalshi_predictor.overnight_paper.runtime_owner import RuntimeOwner, validate_runtime_owner
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.overnight_paper.store import digest
from kalshi_predictor.paper.models import PaperDecision
from kalshi_predictor.utils.time import utc_now

# Windows FILE_ATTRIBUTE_REPARSE_POINT; also permits scoped typing on POSIX.
_REPARSE_POINT_ATTRIBUTE = 0x400


def persist_cf_cost_block(
    *, session_factory: sessionmaker[Session], database_path: Path,
    decision: PaperDecision, decision_at: datetime, cost_record: dict[str, Any],
    account_identity_sha256: str, runtime_owner: RuntimeOwner,
    authorization: LocalPaperAuthorization, objective_bytes: bytes, settings: Settings,
) -> str:
    """Replay costs against real stored forecast/market rows, then journal a block.

    Source hashes are references from the persisted forecast, not proof of full
    provenance or model skill. No fabricated Phase 3M/3N result is supplied.
    The outer producer owns the runtime lock. This writer neither forecasts nor
    calls sizing, risk, qualification, activation, or the order simulator.
    """
    assert_public_only_settings(settings)
    if (
        settings.execution_enabled or not settings.execution_dry_run
        or not settings.execution_kill_switch or settings.execution_gateway_mode != "disabled"
        or settings.autopilot_enabled or not settings.autopilot_dry_run
        or settings.paper_order_creation_enabled or not settings.paper_order_kill_switch
    ):
        raise ValueError("CF_PREPARATION_ORDERS_DISABLED_REQUIRED")
    path = database_path.resolve(strict=True)
    if "onedrive" in str(path).lower() or any(
        p.is_symlink()
        or getattr(p.lstat(), "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
        for p in (database_path, *database_path.parents)
    ):
        raise ValueError("CF_PREPARATION_ISOLATED_DATABASE_REQUIRED")
    now, at = utc_now(), aware(decision_at)
    if (
        type(authorization) is not LocalPaperAuthorization
        or authorization.mode != ExecutionMode.LOCAL_PAPER
        or authorization.max_new_positions != 1 or authorization.max_contracts_per_position != 1
        or not 0 < len(objective_bytes) <= 1_000_000
        or hashlib.sha256(objective_bytes).hexdigest() != authorization.objective_sha256
        or authorization.isolated_database_path is None
        or Path(authorization.isolated_database_path).resolve(strict=True) != path
        or not aware(authorization.created_at) <= now < aware(authorization.expires_at)
        or not 0 <= (now-at).total_seconds() <= 60
    ):
        raise ValueError("CF_PREPARATION_CURRENT_AUTHORIZATION_REQUIRED")
    validate_runtime_owner(runtime_owner, database_path)
    owned = _owned_session_factory(session_factory, path)
    with owned() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        _verify_database(session, path)
        forecast = session.get(Forecast, decision.forecast_id) if decision.forecast_id else None
        market = session.get(Market, decision.ticker)
        if forecast is None or market is None:
            raise ValueError("CF_PREPARATION_STORED_INPUTS_REQUIRED")
        generated = forecast.forecasted_at
        if generated.tzinfo is None:  # SQLAlchemy SQLite stores UTC without its offset.
            generated = generated.replace(tzinfo=UTC)
        if not 0 <= (at-generated).total_seconds() <= 60:
            raise ValueError("CF_PREPARATION_CURRENT_FORECAST_REQUIRED")
        scope = cf_risk_cost_scope(
            forecast=forecast, market=market, decision=decision, at=at,
            account_identity_sha256=account_identity_sha256,
        )
        if cost_record.get("request", {}).get("account_identity_sha256") != account_identity_sha256:
            raise ValueError("CF_PREPARATION_ACCOUNT_IDENTITY_MISMATCH")
        identity = digest(scope)
        payload = preparation_cost_block_record(scope, cost_record)
        validate_runtime_owner(runtime_owner, database_path)
        current = utc_now()
        if (
            not 0 <= (current-at).total_seconds() <= 60
            or not aware(authorization.created_at) <= current < aware(authorization.expires_at)
        ):
            raise ValueError("CF_PREPARATION_EXPIRED_BEFORE_WRITE")
        _checkpoint(session, PREFIX+identity, at, payload)
        session.commit()
    return identity
