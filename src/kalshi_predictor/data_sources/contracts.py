"""Captured source health, separate from authentication and trading readiness.

These metadata contracts consume trusted capture/rights registry outputs. They
do not verify raw artifacts, certify settlement applicability, or replace the
existing overnight forecast-window and provenance gates. Unknown clocks remain
unknown; a successful request alone never establishes analytical freshness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from kalshi_predictor.phase4cd.rights import corpus_access_status


class HealthStatus(StrEnum):
    READY_FRESH = "READY_FRESH"
    READY_REUSED_FRESH = "READY_REUSED_FRESH"
    STALE = "STALE"
    AUTH_FAILURE = "AUTH_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"
    API_FAILURE = "API_FAILURE"
    NO_MATCHING_MARKETS = "NO_MATCHING_MARKETS"
    RIGHTS_PENDING = "RIGHTS_PENDING"
    DISABLED = "DISABLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class CredentialStatus(StrEnum):
    CONFIGURED = "CONFIGURED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_REQUIRED = "NOT_REQUIRED"


class ConfigurationSource(StrEnum):
    ENVIRONMENT = "ENVIRONMENT"
    LOCAL_SECRET_FILE = "LOCAL_SECRET_FILE"
    KEYRING = "KEYRING"
    CONFIGURATION = "CONFIGURATION"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CaptureOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    AUTH_FAILURE = "AUTH_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"
    API_FAILURE = "API_FAILURE"
    NO_MATCHING_MARKETS = "NO_MATCHING_MARKETS"


class ClockBasis(StrEnum):
    PROVIDER_TIMESTAMP = "PROVIDER_TIMESTAMP"
    PUBLIC_REST_RECEIPT = "PUBLIC_REST_RECEIPT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CredentialMetadata:
    """Availability metadata only; no secret value or authentication claim."""

    provider_name: str
    status: CredentialStatus
    alias_used: str | None
    configuration_source: ConfigurationSource
    notice: str | None = None
    data_access_available: bool = False


@dataclass(frozen=True)
class FreshnessPolicy:
    """Explicit use-specific limits; no catalog-wide inferred default."""

    provider_max_age_seconds: int
    receipt_max_age_seconds: int

    def __post_init__(self) -> None:
        for value in (self.provider_max_age_seconds, self.receipt_max_age_seconds):
            if type(value) is not int or value <= 0:
                raise ValueError("POSITIVE_EXPLICIT_FRESHNESS_LIMIT_REQUIRED")


@dataclass(frozen=True)
class SourceDefinition:
    provider_name: str
    category: str
    priority_tier: int
    credential_required: bool
    credential_aliases: tuple[str, ...]
    supported_features: tuple[str, ...]
    supported_market_families: tuple[str, ...]
    rights_corpus: str | None = None
    freshness_limit: FreshnessPolicy | None = None


@dataclass(frozen=True)
class SourceProvenance:
    provider_name: str
    original_sha256: str
    endpoint_id: str

    def valid_for(self, provider_name: str) -> bool:
        # Only stable endpoint identifiers, never URLs/headers/query credentials.
        return (
            self.provider_name == provider_name
            and re.fullmatch(r"[a-f0-9]{64}", self.original_sha256) is not None
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", self.endpoint_id) is not None
        )


@dataclass(frozen=True)
class CapturedSourceEvidence:
    provider_name: str
    outcome: CaptureOutcome
    latest_provider_timestamp: datetime | None
    latest_receipt_timestamp: datetime | None
    provenance: SourceProvenance | None
    clock_basis: ClockBasis = ClockBasis.UNKNOWN
    http_status: int | None = None
    schema_validated: bool = False
    record_count: int | None = None
    reused: bool = False
    previous_original_sha256: str | None = None


@dataclass(frozen=True)
class RegisteredRights:
    """Metadata from an explicit authorization already in the rights registry.

    Constructing this record does not register authorization. The inventory/UI
    caller must obtain it from the trusted registered authorization artifact;
    source key availability and successful HTTP requests are not rights grants.
    """

    provider_name: str
    corpus_name: str
    authorization_sha256: str
    registered_at: datetime
    allowed_use: str
    expires_at: datetime | None = None

    def valid_for(self, definition: SourceDefinition, now: datetime) -> bool:
        try:
            reference, registered = _utc(now), _utc(self.registered_at)
            if self.expires_at is not None and reference >= _utc(self.expires_at):
                return False
        except ValueError:
            return False
        return (
            self.provider_name == definition.provider_name
            and self.corpus_name == definition.rights_corpus
            and self.allowed_use == "RESEARCH_INGESTION"
            and registered <= reference
            and re.fullmatch(r"[a-f0-9]{64}", self.authorization_sha256) is not None
        )


@dataclass(frozen=True)
class SourceHealth:
    provider_name: str
    category: str
    credential_required: bool
    credential_status: CredentialStatus
    health_status: HealthStatus
    reason: str
    latest_provider_timestamp: datetime | None
    latest_receipt_timestamp: datetime | None
    freshness_limit: FreshnessPolicy | None
    provenance: SourceProvenance | None
    supported_features: tuple[str, ...]
    supported_market_families: tuple[str, ...]
    provider_age_seconds: float | None = None
    receipt_age_seconds: float | None = None
    capture_outcome: CaptureOutcome | None = None


def _utc(value: datetime | None) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_CAPTURE_TIMESTAMP_REQUIRED")
    return value.astimezone(UTC)


def assess_source(
    definition: SourceDefinition,
    credential: CredentialMetadata,
    evidence: CapturedSourceEvidence | None,
    *,
    now: datetime,
    freshness_limit: FreshnessPolicy | None = None,
    rights: RegisteredRights | None = None,
    enabled: bool = True,
) -> SourceHealth:
    """Deterministic metadata assessment with no clock repair or I/O.

    READY describes captured data under the supplied policy, never model value,
    corpus ingestion authority, settlement certification or paper eligibility.
    """
    policy = freshness_limit if freshness_limit is not None else definition.freshness_limit
    provider_age = receipt_age = None

    def result(status: HealthStatus, reason: str) -> SourceHealth:
        return SourceHealth(
            definition.provider_name,
            definition.category,
            definition.credential_required,
            credential.status,
            status,
            reason,
            evidence.latest_provider_timestamp if evidence else None,
            evidence.latest_receipt_timestamp if evidence else None,
            policy,
            evidence.provenance if evidence else None,
            definition.supported_features,
            definition.supported_market_families,
            provider_age,
            receipt_age,
            evidence.outcome if evidence else None,
        )

    if credential.provider_name != definition.provider_name:
        return result(HealthStatus.API_FAILURE, "CREDENTIAL_METADATA_PROVIDER_MISMATCH")
    if not enabled:
        return result(HealthStatus.DISABLED, "SOURCE_DISABLED")
    if definition.rights_corpus is not None:
        authorized = rights is not None and rights.valid_for(definition, now)
        legacy = corpus_access_status(definition.rights_corpus, authorization_registered=authorized)
        if not authorized or legacy == "BLOCKED_PENDING_RIGHTS":
            return result(HealthStatus.RIGHTS_PENDING, "REGISTERED_RESEARCH_RIGHTS_REQUIRED")
    if definition.credential_required and credential.status != CredentialStatus.CONFIGURED:
        return result(HealthStatus.NOT_CONFIGURED, "REQUIRED_CREDENTIAL_NOT_CONFIGURED")
    if definition.credential_required and credential.data_access_available is not True:
        return result(HealthStatus.NOT_CONFIGURED, "DATA_ACCESS_CREDENTIAL_NOT_CONFIGURED")
    if evidence is None:
        return result(HealthStatus.API_FAILURE, "NO_CAPTURE_EVIDENCE")
    if evidence.provider_name != definition.provider_name:
        return result(HealthStatus.API_FAILURE, "CAPTURE_PROVIDER_MISMATCH")
    if evidence.http_status in {401, 403} or evidence.outcome == CaptureOutcome.AUTH_FAILURE:
        return result(HealthStatus.AUTH_FAILURE, "CAPTURED_AUTHENTICATION_OR_ACCESS_FAILURE")
    if evidence.http_status == 429 or evidence.outcome == CaptureOutcome.RATE_LIMITED:
        return result(HealthStatus.RATE_LIMITED, "CAPTURED_RATE_LIMIT")
    if evidence.outcome == CaptureOutcome.API_FAILURE:
        return result(HealthStatus.API_FAILURE, "CAPTURED_API_FAILURE")
    if type(evidence.http_status) is not int or not 200 <= evidence.http_status < 300:
        return result(HealthStatus.API_FAILURE, "SUCCESS_HTTP_STATUS_NOT_CAPTURED")
    if evidence.schema_validated is not True:
        return result(HealthStatus.API_FAILURE, "CAPTURE_SCHEMA_NOT_VALIDATED")
    if evidence.provenance is None or not evidence.provenance.valid_for(definition.provider_name):
        return result(HealthStatus.API_FAILURE, "CAPTURE_PROVENANCE_INVALID")
    if type(evidence.record_count) is not int or evidence.record_count < 0:
        return result(HealthStatus.API_FAILURE, "CAPTURE_RECORD_COUNT_UNKNOWN")
    if evidence.outcome == CaptureOutcome.NO_MATCHING_MARKETS or evidence.record_count == 0:
        return result(HealthStatus.NO_MATCHING_MARKETS, "CAPTURE_HAS_NO_MATCHING_RECORDS")
    if evidence.outcome != CaptureOutcome.SUCCESS:
        return result(HealthStatus.API_FAILURE, "CAPTURE_OUTCOME_UNKNOWN")
    if policy is None:
        return result(HealthStatus.STALE, "FRESHNESS_POLICY_NOT_CONFIGURED")
    if evidence.clock_basis != ClockBasis.PROVIDER_TIMESTAMP:
        return result(HealthStatus.STALE, "PROVIDER_CLOCK_UNKNOWN_RECEIPT_IS_NOT_PROVIDER_TIME")
    try:
        reference = _utc(now)
        provider = _utc(evidence.latest_provider_timestamp)
        receipt = _utc(evidence.latest_receipt_timestamp)
    except ValueError:
        return result(HealthStatus.STALE, "PROVIDER_OR_RECEIPT_TIMESTAMP_UNKNOWN_OR_INVALID")
    provider_age = (reference - provider).total_seconds()
    receipt_age = (reference - receipt).total_seconds()
    if provider > receipt or receipt > reference:
        return result(HealthStatus.STALE, "CAPTURE_CLOCK_ORDER_INVALID")
    if provider_age > policy.provider_max_age_seconds:
        return result(HealthStatus.STALE, "PROVIDER_TIMESTAMP_STALE")
    if receipt_age > policy.receipt_max_age_seconds:
        return result(HealthStatus.STALE, "RECEIPT_TIMESTAMP_STALE")
    if evidence.reused:
        if evidence.previous_original_sha256 != evidence.provenance.original_sha256:
            return result(HealthStatus.API_FAILURE, "REUSED_ORIGINAL_HASH_MISMATCH")
        return result(
            HealthStatus.READY_REUSED_FRESH, "ORIGINAL_REUSED_WITHIN_BOTH_FRESHNESS_LIMITS"
        )
    return result(HealthStatus.READY_FRESH, "CAPTURE_WITHIN_BOTH_FRESHNESS_LIMITS")
