"""Immutable rule evidence; unknown effective boundaries never mean unbounded validity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from .source_health import aware


@dataclass(frozen=True)
class WeatherSettlementRule:
    series: str
    provider: str
    effective_from: str | None
    effective_to: str | None
    station: str | None
    metric: str | None
    observation_semantics: str | None
    precision: str | None
    rounding: str | None
    finality: str | None
    conversion: str | None
    missing_corrected_handling: str | None
    network_sensor: str | None
    first_event: str | None
    evidence_urls: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    conflicts: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        body = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    @property
    def missing_fields(self) -> tuple[str, ...]:
        required = (
            "effective_from",
            "effective_to",
            "station",
            "metric",
            "observation_semantics",
            "precision",
            "rounding",
            "finality",
            "conversion",
            "missing_corrected_handling",
            "network_sensor",
            "first_event",
            "evidence_urls",
            "evidence_hashes",
        )
        return tuple(name for name in required if not getattr(self, name))


@dataclass(frozen=True)
class RuleSelection:
    status: str
    series: str
    rule_version: str | None
    provider: str | None
    blockers: tuple[str, ...]


def select_weather_rule(
    registry: tuple[WeatherSettlementRule, ...],
    *,
    series: str,
    observation_time: str | datetime,
    provider: str | None = None,
) -> RuleSelection:
    """Block only the requested family on unknown boundaries or conflicting versions."""
    rows = [row for row in registry if row.series == series]
    if not rows:
        return RuleSelection("UNSUPPORTED", series, None, None, ("NO_WEATHER_RULE",))
    try:
        target = aware(observation_time)
        if any(not row.effective_from or not row.effective_to for row in rows):
            return RuleSelection("BLOCKED", series, None, None, ("CUTOVER_BOUNDARY_UNKNOWN",))
        if any(aware(row.effective_from) >= aware(row.effective_to) for row in rows):
            raise ValueError("INVALID_INTERVAL")
        matches = [
            row for row in rows if aware(row.effective_from) <= target < aware(row.effective_to)
        ]
    except (ValueError, TypeError, AttributeError):
        return RuleSelection("BLOCKED", series, None, None, ("RULE_TIMESTAMP_INVALID",))
    if any(row.conflicts for row in rows) or len(matches) > 1:
        return RuleSelection("BLOCKED", series, None, None, ("SETTLEMENT_RULE_CONFLICT",))
    if not matches:
        return RuleSelection("BLOCKED", series, None, None, ("NO_EFFECTIVE_RULE",))
    row = matches[0]
    if provider is not None and provider != row.provider:
        return RuleSelection("BLOCKED", series, None, None, ("PROVIDER_CONFLICT",))
    if row.missing_fields:
        return RuleSelection(
            "BLOCKED",
            series,
            row.version,
            row.provider,
            tuple("MISSING_" + name.upper() for name in row.missing_fields),
        )
    return RuleSelection("CERTIFIED", series, row.version, row.provider, ())


def knyc_incomplete_registry(series_hash: str) -> tuple[WeatherSettlementRule, ...]:
    """Captured claims only. The September 9 notice has no precise effective instant."""
    urls = (
        "https://external-api.kalshi.com/trade-api/v2/series/KXTEMPNYCH",
        "https://help.kalshi.com/en/articles/13823837-weather-markets",
    )
    twc = WeatherSettlementRule(
        provider="The Weather Company",
        observation_semantics="Value at exact named contract time; sampling selection unknown",
        finality="TWC final reported value; correction horizon unknown",
        series="KXTEMPNYCH",
        effective_from=None,
        effective_to=None,
        station="KNYC",
        metric="temperature",
        precision=None,
        rounding=None,
        conversion=None,
        missing_corrected_handling=None,
        network_sensor=None,
        first_event=None,
        evidence_urls=urls,
        evidence_hashes=(series_hash,),
    )
    return (
        twc,
        replace(
            twc,
            provider="Synoptic Data",
            observation_semantics=None,
            finality=None,
        ),
    )
