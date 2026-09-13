"""Exact, offline CF window arithmetic; never certifies rules or final settlement.

Callers must supply contract-specific boundaries and rounding, supported by rule
evidence. Kalshi's trailing [t-60s,t) and quarter-hour (t-60s,t] feed averages
are different: https://docs.kalshi.com/websockets/cfbenchmarks-value .
CF amendTime is publication time of an amendment, not the observation time:
https://docs.cfbenchmarks.com/api/rest/latest-values/ .
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Literal

Rounding = Literal["HALF_UP", "HALF_EVEN", "DOWN", "FLOOR", "CEILING"]
Amendments = Literal["REJECT", "LATEST_KNOWN_AS_OF"]


@dataclass(frozen=True)
class CFWindow:
    start_ms: int
    end_ms: int
    include_start: bool
    include_end: bool
    cadence_ms: int
    expected_ticks: int

    def timestamps(self) -> tuple[int, ...]:
        for value in (self.start_ms, self.end_ms, self.cadence_ms, self.expected_ticks):
            if type(value) is not int:
                raise ValueError("INVALID_WINDOW_INTEGER")
        if type(self.include_start) is not bool or type(self.include_end) is not bool:
            raise ValueError("BOUNDARY_CONVENTION_REQUIRED")
        if (
            self.start_ms < 0 or self.end_ms <= self.start_ms
            or self.cadence_ms != 1000 or not 1 <= self.expected_ticks <= 3600
            or self.start_ms % self.cadence_ms or self.end_ms % self.cadence_ms
        ):
            raise ValueError("INVALID_SECOND_WINDOW")
        first = self.start_ms + (0 if self.include_start else self.cadence_ms)
        stop = self.end_ms + (self.cadence_ms if self.include_end else 0)
        count = (stop - first) // self.cadence_ms
        if count != self.expected_ticks:
            raise ValueError("EXPECTED_TICK_COUNT_MISMATCH")
        return tuple(range(first, stop, self.cadence_ms))


@dataclass(frozen=True)
class CFWindowRules:
    market_ticker: str
    index_id: str
    closing: CFWindow
    opening: CFWindow | None
    decimal_places: int
    rounding: Rounding
    amendments: Amendments
    rule_source: str
    rule_sha256: str


@dataclass(frozen=True)
class CFTick:
    index_id: str
    time_ms: int
    value: Decimal
    received_ms: int
    amend_time_ms: int | None
    source_sha256: str
    repeat_of_previous_value: bool = False


@dataclass(frozen=True)
class CFWindowValue:
    value: Decimal
    exact_mean_numerator: int
    exact_mean_denominator: int
    tick_count: int
    amended_tick_count: int
    selected_ticks_sha256: str


@dataclass(frozen=True)
class CFReconstruction:
    market_ticker: str
    index_id: str
    as_of_ms: int
    opening: CFWindowValue | None
    closing: CFWindowValue
    rules: CFWindowRules
    rules_sha256: str
    # Mathematical reconstruction is not the exchange's authoritative result.
    status: str = "RECONSTRUCTED_NOT_CERTIFIED_SETTLEMENT"


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _rounded(mean: Fraction, places: int, mode: Rounding) -> Decimal:
    # Round the rational directly: Decimal division can round before a tie test.
    numerator = mean.numerator * 10**places
    whole, remainder = divmod(numerator, mean.denominator)
    twice = remainder * 2
    if (
        (mode == "CEILING" and remainder > 0)
        or (mode == "HALF_UP" and twice >= mean.denominator)
        or (mode == "HALF_EVEN" and (
            twice > mean.denominator or (twice == mean.denominator and whole % 2)
        ))
    ):
        whole += 1
    digits = tuple(int(char) for char in str(whole))
    return Decimal((0, digits, -places))


def reconstruct_cf_windows(
    rules: CFWindowRules, ticks: tuple[CFTick, ...], *, as_of_ms: int,
) -> CFReconstruction:
    """Require every expected one-second tick known by the receipt/publication cutoff.

    Unknown boundaries/rounding must not be substituted with defaults. Explicit
    amendments can replace an observation only with LATEST_KNOWN_AS_OF; duplicate
    revision timestamps, synthetic repeat values, and off-grid ticks fail closed.
    External callers remain responsible for verifying the supplied rule evidence.
    """
    if type(as_of_ms) is not int or as_of_ms < 0:
        raise ValueError("INVALID_AS_OF")
    if (
        not rules.market_ticker or not rules.index_id or not rules.rule_source
        or not _valid_hash(rules.rule_sha256)
        or type(rules.decimal_places) is not int or not 0 <= rules.decimal_places <= 18
        or rules.rounding not in {"HALF_UP", "HALF_EVEN", "DOWN", "FLOOR", "CEILING"}
        or rules.amendments not in {"REJECT", "LATEST_KNOWN_AS_OF"}
    ):
        raise ValueError("EXPLICIT_SUPPORTED_RULES_REQUIRED")
    closing = rules.closing.timestamps()
    opening = rules.opening.timestamps() if rules.opening else ()
    if opening and opening[-1] >= closing[0]:
        raise ValueError("OPENING_CLOSING_OVERLAP")
    if as_of_ms < closing[-1]:
        raise ValueError("WINDOW_NOT_COMPLETE_AS_OF")
    expected = set(opening + closing)
    versions: dict[int, dict[int, CFTick]] = {}
    for tick in ticks:
        if tick.index_id != rules.index_id:
            raise ValueError("INDEX_MISMATCH")
        if (
            type(tick.time_ms) is not int or type(tick.received_ms) is not int
            or tick.time_ms < 0 or tick.received_ms < tick.time_ms
            or (tick.amend_time_ms is not None and (
                type(tick.amend_time_ms) is not int
                or not tick.time_ms < tick.amend_time_ms <= tick.received_ms
            ))
        ):
            raise ValueError("INVALID_TICK_TIME")
        if tick.received_ms > as_of_ms:
            continue
        inside = any(
            window.start_ms < tick.time_ms < window.end_ms
            for window in (rules.closing, rules.opening) if window is not None
        )
        if tick.time_ms not in expected:
            if inside:
                raise ValueError("OFF_GRID_TICK")
            continue
        if (
            not isinstance(tick.value, Decimal) or not tick.value.is_finite()
            or tick.value <= 0 or len(tick.value.as_tuple().digits) > 128
            or abs(tick.value.adjusted()) > 128
            or not _valid_hash(tick.source_sha256) or tick.repeat_of_previous_value
        ):
            raise ValueError("INVALID_OR_SYNTHETIC_TICK")
        if tick.amend_time_ms is not None and rules.amendments == "REJECT":
            raise ValueError("AMENDMENT_POLICY_REJECTED")
        revision = tick.amend_time_ms if tick.amend_time_ms is not None else tick.time_ms
        timestamp_versions = versions.setdefault(tick.time_ms, {})
        if revision in timestamp_versions:
            raise ValueError("DUPLICATE_TICK_REVISION")
        timestamp_versions[revision] = tick
    if set(versions) != expected:
        raise ValueError("INCOMPLETE_WINDOW")

    def average(timestamps: tuple[int, ...]) -> CFWindowValue:
        selected = [versions[time][max(versions[time])] for time in timestamps]
        mean = sum((Fraction(tick.value) for tick in selected), Fraction()) / len(selected)
        return CFWindowValue(
            value=_rounded(mean, rules.decimal_places, rules.rounding),
            exact_mean_numerator=mean.numerator,
            exact_mean_denominator=mean.denominator,
            tick_count=len(selected),
            amended_tick_count=sum(tick.amend_time_ms is not None for tick in selected),
            selected_ticks_sha256=_hash([asdict(tick) for tick in selected]),
        )

    return CFReconstruction(
        market_ticker=rules.market_ticker, index_id=rules.index_id, as_of_ms=as_of_ms,
        opening=average(opening) if opening else None, closing=average(closing),
        rules=rules, rules_sha256=_hash(asdict(rules)),
    )
