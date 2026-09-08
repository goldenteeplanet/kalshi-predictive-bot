"""Pinned, reviewed settlement policies; artifact integrity is not certification.

No production family is certified yet. Adding a policy requires review of the
original authoritative documents and an implemented observation methodology.
The registry is a code dependency, never loaded from candidate JSON.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any
from urllib.parse import urlsplit

from .provenance import canonical_hash
from .source_health import aware


@dataclass(frozen=True)
class RuleDocument:
    url: str
    payload: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class CertifiedRulePolicy:
    ticker: str
    event_id: str
    series: str
    provider: str
    source_identity: str
    observation_time: str
    selection: str
    conversion: str
    precision: str
    rounding: str
    finality: str
    effective_from: str
    effective_to: str
    # Ordered (original authoritative URL, SHA256) pairs, including amendments.
    documents: tuple[tuple[str, str], ...]
    amendments: tuple[str, ...]
    methodology: str
    expected_settlement_seconds: int
    final_settlement_seconds: int
    review_extension_seconds: int | None

    @property
    def version(self) -> str:
        from dataclasses import asdict

        return canonical_hash(asdict(self))


@dataclass(frozen=True)
class RuleVerification:
    passed: bool
    blockers: tuple[str, ...]
    rule_version: str | None = None
    policy: CertifiedRulePolicy | None = None


# Empty intentionally: captured NYC and crypto rules still have evidence gaps.
CERTIFIED_RULE_POLICIES: tuple[CertifiedRulePolicy, ...] = ()
# Methods do not certify any production policy or resolve undocumented rounding.
CF_MINUTE_METHOD = "cf-standard-60-top-of-second-mean-v1"
CF_ROUNDING = frozenset({"ROUND_HALF_UP", "ROUND_HALF_EVEN"})
IMPLEMENTED_METHODOLOGIES = frozenset({"exact-timestamp-decimal-v1", CF_MINUTE_METHOD})


def verify_settlement_rule(
    *,
    decision: dict[str, Any],
    documents: tuple[RuleDocument, ...],
    registry: tuple[CertifiedRulePolicy, ...] = CERTIFIED_RULE_POLICIES,
) -> RuleVerification:
    """Recompute certification from the pinned policy and original documents.

    The decision carries settlement_rule with every semantic field below, plus
    top-level rule_version and observation_time. Supplied verdicts have no effect.
    Effective intervals are half-open; unknown review extensions fail closed.
    """
    try:
        matches = [p for p in registry if p.ticker == decision.get("ticker")]
        if len(matches) != 1:
            raise ValueError("NO_UNAMBIGUOUS_CERTIFIED_RULE")
        policy = matches[0]
        if decision.get("event_id") != policy.event_id or decision.get("series") != policy.series:
            raise ValueError("RULE_IDENTITY_MISMATCH")
        if decision.get("rule_version") != policy.version:
            raise ValueError("RULE_VERSION_MISMATCH")
        observation = aware(decision["observation_time"])
        start, end = aware(policy.effective_from), aware(policy.effective_to)
        if not start <= observation < end or observation != aware(policy.observation_time):
            raise ValueError("RULE_NOT_EFFECTIVE_AT_OBSERVATION")
        if aware(decision["decision_at"]) >= observation:
            raise ValueError("OBSERVATION_NOT_IN_FUTURE")
        semantic_fields = (
            "provider",
            "source_identity",
            "selection",
            "conversion",
            "precision",
            "rounding",
            "finality",
            "effective_from",
            "effective_to",
            "methodology",
        )
        claim = decision.get("settlement_rule")
        if not isinstance(claim, dict):
            raise ValueError("RULE_SEMANTICS_MISSING")
        for field in semantic_fields:
            expected = getattr(policy, field)
            if not isinstance(expected, str) or not expected or claim.get(field) != expected:
                raise ValueError("RULE_SEMANTIC_MISMATCH:" + field)
        if claim.get("amendments") != list(policy.amendments):
            raise ValueError("RULE_AMENDMENTS_MISMATCH")
        if policy.methodology not in IMPLEMENTED_METHODOLOGIES:
            raise ValueError("RULE_METHODOLOGY_NOT_IMPLEMENTED")
        if policy.methodology == "exact-timestamp-decimal-v1":
            if (policy.selection, policy.conversion, policy.precision, policy.rounding) != (
                "exact timestamp",
                "identity",
                "original decimal",
                "none",
            ):
                raise ValueError("RULE_NUMERICAL_METHOD_NOT_IMPLEMENTED")
        elif (
            policy.selection != "60 standard top-of-second points in [observation-60s,observation)"
            or policy.conversion != "identity USD"
            or policy.precision != "input USD 0.01; arithmetic mean; output USD 0.01"
            or policy.rounding not in CF_ROUNDING
            or policy.provider != "CF Benchmarks"
            or policy.source_identity != "BRTI"
        ):
            raise ValueError("RULE_CF_NUMERICAL_SEMANTICS_UNCERTIFIED")
        if not policy.documents or len(set(policy.documents)) != len(policy.documents):
            raise ValueError("RULE_ORIGINAL_DOCUMENTS_REQUIRED")
        originals = tuple((doc.url, doc.sha256) for doc in documents)
        if originals != policy.documents or any(not doc.payload for doc in documents):
            raise ValueError("RULE_ORIGINAL_DOCUMENT_MISMATCH")
        if any(
            urlsplit(url).scheme != "https"
            or urlsplit(url).hostname
            not in {
                "kalshi.com",
                "help.kalshi.com",
                "assets.kalshi.com",
                "docs.cfbenchmarks.com",
                "www.cfbenchmarks.com",
                "kalshi-public-docs.s3.amazonaws.com",
            }
            for url, _ in policy.documents
        ):
            raise ValueError("RULE_DOCUMENT_AUTHORITY_UNSUPPORTED")
        if any(item not in {sha for _, sha in policy.documents} for item in policy.amendments):
            raise ValueError("RULE_AMENDMENT_ORIGINAL_MISSING")
        if (
            type(policy.expected_settlement_seconds) is not int
            or type(policy.final_settlement_seconds) is not int
            or type(policy.review_extension_seconds) is not int
            or not 0 <= policy.expected_settlement_seconds <= policy.final_settlement_seconds
            or policy.review_extension_seconds < 0
        ):
            raise ValueError("RULE_SETTLEMENT_FINALITY_UNBOUNDED")
        return RuleVerification(True, (), policy.version, policy)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return RuleVerification(False, (str(exc) or "RULE_EVIDENCE_INVALID",))


def select_exact_observation(
    observations: tuple[tuple[str, str], ...],
    *,
    observation_time: str,
) -> str:
    """Select exactly one original decimal reading; do not interpolate or round."""
    from decimal import Decimal

    target = aware(observation_time)
    values = [value for timestamp, value in observations if aware(timestamp) == target]
    if len(values) != 1 or not Decimal(values[0]).is_finite():
        raise ValueError("EXACT_OBSERVATION_MISSING_OR_AMBIGUOUS")
    return values[0]


@dataclass(frozen=True)
class MinuteAverage:
    """Exact rational mean; optional cents rounded under an explicit policy choice."""

    window_start: datetime
    window_end_exclusive: datetime
    point_count: int
    unrounded_mean: Fraction
    rounded_value: Decimal | None
    rounding: str | None


def evaluate_cf_minute(
    observations: tuple[tuple[str | datetime, str | Decimal], ...],
    *,
    boundary: str | datetime,
    rounding: str | None = None,
) -> MinuteAverage:
    """Evaluate one complete standard BRTI minute, never resample a 5Hz stream.

    Input must already be exactly the requested half-open 60-point window. No
    sorting, deduplication, interpolation, last-value fill, or float conversion.
    None returns only an exact rational mean for research; qualification requires
    a separately reviewed pinned rule with an explicit supported rounding choice.
    """
    end = aware(boundary)
    if end.microsecond or len(observations) != 60:
        raise ValueError("CF_EXACT_60_POINT_WINDOW_REQUIRED")
    if rounding is not None and rounding not in CF_ROUNDING:
        raise ValueError("CF_ROUNDING_CONVENTION_REQUIRED")
    start = end - timedelta(seconds=60)
    total_cents = 0
    for offset, (timestamp, value) in enumerate(observations):
        if aware(timestamp) != start + timedelta(seconds=offset):
            raise ValueError("CF_TOP_OF_SECOND_GRID_REQUIRED")
        if not isinstance(value, str | Decimal):
            raise ValueError("CF_ORIGINAL_DECIMAL_REQUIRED")
        try:
            decimal = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("CF_DECIMAL_VALUE_INVALID") from exc
        if not decimal.is_finite() or decimal <= 0:
            raise ValueError("CF_POSITIVE_FINITE_VALUE_REQUIRED")
        cents = Fraction(decimal) * 100
        if cents.denominator != 1:
            raise ValueError("CF_STANDARD_BRTI_CENT_PRECISION_REQUIRED")
        total_cents += cents.numerator
    mean = Fraction(total_cents, 6000)
    rounded_value = None
    if rounding is not None:
        # Integer arithmetic makes exact half-cent ties independent of ambient
        # Decimal precision and rounding context, even for repeating means.
        whole_cents, remainder = divmod(total_cents, 60)
        if remainder > 30 or (remainder == 30 and (rounding == "ROUND_HALF_UP" or whole_cents % 2)):
            whole_cents += 1
        rounded_value = Decimal(f"{whole_cents // 100}.{whole_cents % 100:02d}")
    return MinuteAverage(start, end, 60, mean, rounded_value, rounding)
