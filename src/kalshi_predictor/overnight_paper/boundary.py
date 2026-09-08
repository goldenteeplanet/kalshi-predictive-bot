"""Local-only sprint authorization. This module has no exchange or network imports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class ExecutionMode(StrEnum):
    OBSERVATION_ONLY = "OBSERVATION_ONLY"
    LOCAL_PAPER = "LOCAL_PAPER"
    DEMO_EXCHANGE = "DEMO_EXCHANGE"
    LIVE_EXCHANGE = "LIVE_EXCHANGE"


@dataclass(frozen=True)
class LocalPaperAuthorization:
    created_at: datetime
    expires_at: datetime
    objective_sha256: str
    mode: ExecutionMode = ExecutionMode.LOCAL_PAPER
    max_contracts_per_position: int = 1
    max_new_positions: int = 3
    max_open_positions: int = 3
    max_positions_per_event: int = 1
    hard_horizon_hours: int = 72


def require_local_mode(mode: ExecutionMode) -> None:
    if mode != ExecutionMode.LOCAL_PAPER:
        raise PermissionError("Only LOCAL_PAPER is authorized for local position creation")


def validate_authorization(
    authorization: LocalPaperAuthorization,
    *,
    now: datetime,
    contracts: int,
    new_positions: int,
    open_positions: int,
    event_id: str,
    used_events: frozenset[str],
    settlement_at: datetime,
) -> tuple[str, ...]:
    """Validate prospective creation; counts must be read under the writer transaction.

    This is a scoped record of the supplied operator goal, not a digital signature.
    The caller must verify objective_sha256 against its preserved operator objective.
    """
    errors: list[str] = []
    if authorization.mode != ExecutionMode.LOCAL_PAPER:
        errors.append("LOCAL_PAPER_ONLY")
    if not re.fullmatch(r"[0-9a-f]{64}", authorization.objective_sha256):
        errors.append("OPERATOR_OBJECTIVE_HASH_MISSING")
    bounds = (
        (authorization.max_contracts_per_position, 1),
        (authorization.max_new_positions, 3),
        (authorization.max_open_positions, 3),
        (authorization.max_positions_per_event, 1),
        (authorization.hard_horizon_hours, 72),
    )
    if any(type(value) is not int or not 0 < value <= maximum for value, maximum in bounds):
        return tuple([*errors, "AUTHORIZATION_LIMITS_EXCEEDED"])
    clocks = (now, authorization.created_at, authorization.expires_at, settlement_at)
    if any(value.tzinfo is None or value.utcoffset() is None for value in clocks):
        return tuple([*errors, "TIMEZONE_REQUIRED"])
    if not authorization.created_at <= now < authorization.expires_at:
        errors.append("AUTHORIZATION_NOT_CURRENT")
    if authorization.expires_at - authorization.created_at > timedelta(hours=72):
        errors.append("AUTHORIZATION_DURATION_EXCEEDED")
    if type(contracts) is not int or contracts != 1:
        errors.append("ONE_CONTRACT_MAXIMUM")
    if type(new_positions) is not int or not 0 <= new_positions < min(
        authorization.max_new_positions, 3
    ):
        errors.append("NEW_POSITION_LIMIT")
    if type(open_positions) is not int or not 0 <= open_positions < min(
        authorization.max_open_positions, 3
    ):
        errors.append("OPEN_POSITION_LIMIT")
    if not event_id or event_id in used_events:
        errors.append("INDEPENDENT_EVENT_REQUIRED")
    if not now < settlement_at <= now + timedelta(hours=min(authorization.hard_horizon_hours, 72)):
        errors.append("SETTLEMENT_HORIZON_EXCEEDED")
    return tuple(errors)
