"""Development-only arithmetic plan. No allocation, I/O, lease or admission."""

from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_BYTES = (1 << 63) - 1
PAGE_PHASE_BYTES = 512 * 1024 * 1024
MAX_IDENTITIES = 1024
CHARGED_STATES = frozenset({"RESERVED", "RUNNING", "COMPLETED_CHARGED", "AMBIGUOUS"})


def integer(value, name, maximum=MAX_BYTES):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(name)
    return value


def identity(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or not value.isascii()
        or not all(c.isalnum() or c in "-_." for c in value)
    ):
        raise ValueError("IDENTITY")
    return value


def utc(value):
    if type(value) is not str or len(value) > 40:
        raise ValueError("UTC_TIMESTAMP")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("EXPLICIT_UTC")
    return parsed


@dataclass(frozen=True)
class Charge:
    reservation: str
    filesystem: str
    epoch: str
    state: str
    bytes: int


def plan(
    *,
    filesystem,
    epoch,
    available_bytes,
    observed_at,
    decision_at,
    max_age_seconds,
    protected_margin_bytes,
    pages,
    phases,
    charges,
):
    """Validate bounded supplied evidence and compute a conservative refusal.

    Even POSSIBLE_ARITHMETIC is never permission to launch a writer. Inputs
    are supplied by the caller, not authenticated here. All outstanding full
    charges are subtracted even if reflected in current free space already.
    No release or completion refund is supported.
    """
    filesystem, epoch = identity(filesystem), identity(epoch)
    available = integer(available_bytes, "AVAILABLE_BYTES")
    margin = integer(protected_margin_bytes, "PROTECTED_MARGIN")
    age_limit = integer(max_age_seconds, "AGE_LIMIT", 60)
    if age_limit == 0:
        raise ValueError("POSITIVE_AGE_LIMIT")
    page_count = integer(pages, "PAGES", 120)
    phase_count = integer(phases, "PHASES", 16)
    if not page_count or not phase_count:
        raise ValueError("POSITIVE_PLAN_REQUIRED")
    if type(charges) is not tuple or len(charges) > MAX_IDENTITIES:
        raise ValueError("BOUNDED_CHARGE_TUPLE")
    observed, decision = utc(observed_at), utc(decision_at)
    age = decision - observed
    if age < timedelta(0) or age > timedelta(seconds=age_limit):
        raise ValueError("STALE_OR_FUTURE_OBSERVATION")
    used, seen = 0, set()
    for charge in charges:
        if type(charge) is not Charge:
            raise ValueError("EXACT_CHARGE_REQUIRED")
        key = identity(charge.reservation)
        if key in seen:
            raise ValueError("DUPLICATE_RESERVATION")
        seen.add(key)
        # Other epochs on this filesystem still consume capacity. Never
        # omit their charge merely because the request belongs to a new epoch.
        if identity(charge.filesystem) != filesystem:
            raise ValueError("FILESYSTEM_MISMATCH")
        identity(charge.epoch)
        if type(charge.state) is not str or charge.state not in CHARGED_STATES:
            raise ValueError("UNKNOWN_OR_RELEASED_STATE")
        amount = integer(charge.bytes, "CHARGE_BYTES")
        if amount == 0:
            raise ValueError("POSITIVE_CHARGE_REQUIRED")
        used += amount
        if used > MAX_BYTES:
            raise ValueError("TOTAL_CHARGE_BOUND")
    requested = page_count * phase_count * PAGE_PHASE_BYTES
    needed = used + requested + margin
    if needed > MAX_BYTES:
        raise ValueError("TOTAL_REQUIRED_BOUND")
    return {
        "schema": "CAPACITY_ARITHMETIC_PLAN_V1",
        "status": "POSSIBLE_ARITHMETIC" if available >= needed else "INSUFFICIENT_CAPACITY",
        "filesystem": filesystem,
        "epoch": epoch,
        "observed_at": observed_at,
        "decision_at": decision_at,
        "available_bytes": available,
        "charged_bytes": used,
        "requested_bytes": requested,
        "protected_margin_bytes": margin,
        "remaining_bytes": available - needed,
        "shortfall_bytes": max(0, needed - available),
        "reservation_created": False,
        "writer_authority": False,
        "input_authenticated": False,
        "allocation_enforced": False,
    }
