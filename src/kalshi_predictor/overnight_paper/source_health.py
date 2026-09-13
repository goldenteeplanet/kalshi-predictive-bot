"""Provider-clock health, independent of acquisition time and settlement authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

MAX_FORECAST_AGE_SECONDS = 1800


class SourceState(StrEnum):
    NEW_FRESH_DATA = "NEW_FRESH_DATA"
    REUSED_BUT_FRESH = "REUSED_BUT_FRESH"
    UNCHANGED_AND_FRESH = "UNCHANGED_AND_FRESH"
    STALE = "STALE"
    SOURCE_ERROR = "SOURCE_ERROR"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class SourceHealth:
    state: SourceState
    reason: str
    role: str = "ANALYTICAL_SOURCE"
    provider_age_seconds: float | None = None
    generated_age_seconds: float | None = None

    @property
    def eligible(self) -> bool:
        return self.state in {
            SourceState.NEW_FRESH_DATA,
            SourceState.REUSED_BUT_FRESH,
            SourceState.UNCHANGED_AND_FRESH,
        }


def aware(value: str | datetime | None) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return parsed.astimezone(UTC)


def classify_source(
    *,
    generated_at: str | datetime | None,
    updated_at: str | datetime | None,
    valid_from: str | datetime | None,
    valid_to: str | datetime | None,
    target_start: str | datetime,
    target_end: str | datetime,
    now: datetime,
    payload_hash: str,
    previous_hash: str | None = None,
    reused: bool = False,
    supported: bool = True,
    error: str | None = None,
    max_age_seconds: int = MAX_FORECAST_AGE_SECONDS,
) -> SourceHealth:
    """A single forecast period must cover the whole half-open target interval.

    Callers cannot relax the existing 30-minute bound. Cached and downloaded
    payloads receive identical validation; neither receipt nor hash changes time.
    """
    if not supported:
        return SourceHealth(SourceState.UNSUPPORTED, "SOURCE_NOT_SUPPORTED")
    if error:
        return SourceHealth(SourceState.SOURCE_ERROR, error)
    if not 0 < max_age_seconds <= MAX_FORECAST_AGE_SECONDS:
        return SourceHealth(SourceState.SOURCE_ERROR, "FRESHNESS_LIMIT_REFUSED")
    try:
        reference = aware(now)
        generated, updated = aware(generated_at), aware(updated_at)
        start, end = aware(valid_from), aware(valid_to)
        target, target_stop = aware(target_start), aware(target_end)
    except (ValueError, TypeError, AttributeError):
        return SourceHealth(SourceState.SOURCE_ERROR, "PROVIDER_OR_WINDOW_TIMESTAMP_INVALID")
    ages = ((reference - updated).total_seconds(), (reference - generated).total_seconds())
    if min(ages) < 0:
        return SourceHealth(
            SourceState.SOURCE_ERROR, "PROVIDER_CLOCK_IN_FUTURE", "ANALYTICAL_SOURCE", *ages
        )
    if max(ages) > max_age_seconds:
        return SourceHealth(SourceState.STALE, "PROVIDER_CLOCK_STALE", "ANALYTICAL_SOURCE", *ages)
    if end <= start or target_stop <= target:
        return SourceHealth(SourceState.SOURCE_ERROR, "INVALID_VALIDITY_WINDOW")
    if not start <= target < target_stop <= end:
        return SourceHealth(SourceState.UNSUPPORTED, "TARGET_WINDOW_NOT_COVERED")
    if target_stop <= reference:
        return SourceHealth(SourceState.STALE, "TARGET_WINDOW_EXPIRED")
    if not payload_hash:
        return SourceHealth(SourceState.SOURCE_ERROR, "PAYLOAD_HASH_REQUIRED")
    if reused and previous_hash != payload_hash:
        return SourceHealth(SourceState.SOURCE_ERROR, "CACHE_HASH_MISMATCH")
    state = SourceState.NEW_FRESH_DATA
    if previous_hash == payload_hash:
        state = SourceState.REUSED_BUT_FRESH if reused else SourceState.UNCHANGED_AND_FRESH
    return SourceHealth(
        state, "BOTH_PROVIDER_CLOCKS_AND_TARGET_WINDOW_VALID", "ANALYTICAL_SOURCE", *ages
    )
