from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.data_sources.contracts import (
    CapturedSourceEvidence,
    CaptureOutcome,
    ClockBasis,
    ConfigurationSource,
    CredentialStatus,
    FreshnessPolicy,
    HealthStatus,
    RegisteredRights,
    SourceProvenance,
    assess_source,
)
from kalshi_predictor.data_sources.registry import PROVIDERS, credential_metadata, get_source

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
POLICY = FreshnessPolicy(1800, 60)


def configured(name="BLS"):
    return credential_metadata(
        name,
        available_aliases=get_source(name).credential_aliases[:1],
        configuration_source=ConfigurationSource.LOCAL_SECRET_FILE,
    )


def capture(name="BLS"):
    return CapturedSourceEvidence(
        provider_name=name,
        outcome=CaptureOutcome.SUCCESS,
        latest_provider_timestamp=NOW - timedelta(seconds=30),
        latest_receipt_timestamp=NOW - timedelta(seconds=5),
        provenance=SourceProvenance(name, "a" * 64, "official_series_v2"),
        clock_basis=ClockBasis.PROVIDER_TIMESTAMP,
        http_status=200,
        schema_validated=True,
        record_count=1,
    )


def health(evidence=None, **changes):
    return assess_source(
        get_source("BLS"),
        configured(),
        evidence or capture(),
        now=NOW,
        freshness_limit=POLICY,
        **changes,
    )


def test_exact_health_vocabulary():
    assert {state.value for state in HealthStatus} == {
        "READY_FRESH",
        "READY_REUSED_FRESH",
        "STALE",
        "AUTH_FAILURE",
        "RATE_LIMITED",
        "API_FAILURE",
        "NO_MATCHING_MARKETS",
        "RIGHTS_PENDING",
        "DISABLED",
        "NOT_CONFIGURED",
    }


def test_catalog_contains_named_sources_and_is_read_only():
    assert {
        "BLS",
        "FRED",
        "ALFRED",
        "BEA",
        "SYNOPTIC",
        "DATABENTO",
        "THE_ODDS_API",
        "NEWSAPI_AI",
        "COINGECKO",
        "ALPHA_VANTAGE",
        "UNUSUAL_WHALES",
        "ODDPOOL",
        "PREDICTION_MARKET_BENCH",
        "PREDICTION_MARKETS_PUBLIC",
        "KALSHI",
        "COINBASE",
    } <= PROVIDERS.keys()
    assert all(row.freshness_limit is None for row in PROVIDERS.values())
    with pytest.raises(TypeError):
        PROVIDERS["FAKE"] = get_source("BLS")
    with pytest.raises(ValueError, match="UNREGISTERED"):
        get_source("FAKE")


def test_credential_metadata_alias_precedence_and_no_authentication_claim():
    row = credential_metadata(
        "BLS",
        available_aliases=("BLA API Key.txt",),
        configuration_source=ConfigurationSource.LOCAL_SECRET_FILE,
    )
    assert row.notice == "LEGACY_BLA_ALIAS_ACCEPTED"
    assert row.alias_used == "BLA API Key.txt"
    assert row.status == CredentialStatus.CONFIGURED
    assert "value" not in asdict(row)
    preferred = credential_metadata(
        "BLS",
        available_aliases=("BLA_API_KEY", "BLS_API_KEY"),
        configuration_source=ConfigurationSource.ENVIRONMENT,
    )
    assert preferred.alias_used == "BLS_API_KEY" and preferred.notice is None
    no_capture = assess_source(get_source("BLS"), row, None, now=NOW)
    assert no_capture.health_status == HealthStatus.API_FAILURE
    assert no_capture.reason == "NO_CAPTURE_EVIDENCE"


def test_missing_credentials_and_public_source_require_actual_evidence():
    state = assess_source(get_source("BEA"), credential_metadata("BEA"), None, now=NOW)
    assert state.health_status == HealthStatus.NOT_CONFIGURED
    state = assess_source(get_source("COINBASE"), credential_metadata("COINBASE"), None, now=NOW)
    assert state.credential_status == CredentialStatus.NOT_REQUIRED
    assert state.health_status == HealthStatus.API_FAILURE


def test_synoptic_prefers_data_token_and_management_key_is_not_data_ready():
    management = credential_metadata(
        "SYNOPTIC",
        available_aliases=("SYNOPTIC_API_KEY",),
        configuration_source=ConfigurationSource.LOCAL_SECRET_FILE,
    )
    assert management.status == CredentialStatus.CONFIGURED
    assert management.data_access_available is False
    assert management.notice == "MANAGEMENT_KEY_ONLY_DATA_TOKEN_REQUIRED"
    state = assess_source(
        get_source("SYNOPTIC"), management, capture("SYNOPTIC"), now=NOW, freshness_limit=POLICY
    )
    assert state.health_status == HealthStatus.NOT_CONFIGURED
    assert state.reason == "DATA_ACCESS_CREDENTIAL_NOT_CONFIGURED"
    data = credential_metadata(
        "SYNOPTIC",
        available_aliases=("SYNOPTIC_API_KEY", "SYNOPTIC_TOKEN", "SYNOPTIC_DATA_TOKEN"),
        configuration_source=ConfigurationSource.LOCAL_SECRET_FILE,
    )
    assert data.alias_used == "SYNOPTIC_DATA_TOKEN"
    assert data.data_access_available is True


def test_credential_values_mapping_is_not_accepted():
    with pytest.raises(ValueError, match="NAMES_ONLY"):
        credential_metadata("BLS", available_aliases={"BLS_API_KEY": "do-not-consume"})


def test_fresh_and_reused_keep_original_clocks():
    original = capture()
    fresh = health(original)
    assert fresh.health_status == HealthStatus.READY_FRESH
    assert fresh.provider_age_seconds == 30 and fresh.receipt_age_seconds == 5
    reused = health(replace(original, reused=True, previous_original_sha256="a" * 64))
    assert reused.health_status == HealthStatus.READY_REUSED_FRESH
    assert reused.latest_receipt_timestamp == original.latest_receipt_timestamp
    assert health(replace(original, reused=True)).reason == "REUSED_ORIGINAL_HASH_MISMATCH"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"latest_provider_timestamp": None}, "TIMESTAMP_UNKNOWN"),
        ({"latest_receipt_timestamp": None}, "TIMESTAMP_UNKNOWN"),
        ({"latest_provider_timestamp": NOW.replace(tzinfo=None)}, "TIMESTAMP_UNKNOWN"),
        ({"clock_basis": ClockBasis.PUBLIC_REST_RECEIPT}, "PROVIDER_CLOCK_UNKNOWN"),
        ({"clock_basis": ClockBasis.UNKNOWN}, "PROVIDER_CLOCK_UNKNOWN"),
        ({"latest_provider_timestamp": NOW + timedelta(seconds=1)}, "CLOCK_ORDER_INVALID"),
        ({"latest_receipt_timestamp": NOW + timedelta(seconds=1)}, "CLOCK_ORDER_INVALID"),
        ({"latest_provider_timestamp": NOW - timedelta(seconds=1801)}, "PROVIDER_TIMESTAMP_STALE"),
        (
            {
                "latest_provider_timestamp": NOW - timedelta(seconds=100),
                "latest_receipt_timestamp": NOW - timedelta(seconds=61),
            },
            "RECEIPT_TIMESTAMP_STALE",
        ),
    ],
)
def test_unknown_stale_and_future_clocks_never_ready(changes, reason):
    result = health(replace(capture(), **changes))
    assert result.health_status == HealthStatus.STALE
    assert reason in result.reason


def test_no_default_freshness_policy():
    result = assess_source(get_source("BLS"), configured(), capture(), now=NOW)
    assert result.health_status == HealthStatus.STALE
    assert result.reason == "FRESHNESS_POLICY_NOT_CONFIGURED"


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "AUTH_FAILURE"),
        (403, "AUTH_FAILURE"),
        (429, "RATE_LIMITED"),
        (500, "API_FAILURE"),
        (None, "API_FAILURE"),
    ],
)
def test_captured_errors_override_nominal_success(status, expected):
    result = health(replace(capture(), http_status=status))
    assert result.health_status == expected
    assert result.credential_status == CredentialStatus.CONFIGURED


def test_schema_provenance_and_no_records():
    assert (
        health(replace(capture(), schema_validated=False)).health_status == HealthStatus.API_FAILURE
    )
    assert health(replace(capture(), provenance=None)).health_status == HealthStatus.API_FAILURE
    assert (
        health(replace(capture(), record_count=0)).health_status == HealthStatus.NO_MATCHING_MARKETS
    )
    assert health(replace(capture(), record_count=None)).health_status == HealthStatus.API_FAILURE
    assert health(enabled=False).health_status == HealthStatus.DISABLED
    mismatched = replace(capture(), provenance=SourceProvenance("FRED", "a" * 64, "series"))
    assert health(mismatched).health_status == HealthStatus.API_FAILURE


@pytest.mark.parametrize(
    "name", ["ODDPOOL", "PREDICTION_MARKET_BENCH", "PREDICTION_MARKETS_PUBLIC", "AGENTTRADER"]
)
def test_corpus_success_and_key_do_not_grant_rights(name):
    result = assess_source(
        get_source(name), configured(name), capture(name), now=NOW, freshness_limit=POLICY
    )
    assert result.health_status == HealthStatus.RIGHTS_PENDING


def test_rights_require_matching_registered_scope_and_valid_dates():
    definition = get_source("ODDPOOL")
    rights = RegisteredRights(
        "ODDPOOL", "PredictionMarketBench", "b" * 64, NOW - timedelta(days=1), "RESEARCH_INGESTION"
    )

    def assess(grant):
        return assess_source(
            definition,
            configured("ODDPOOL"),
            capture("ODDPOOL"),
            now=NOW,
            freshness_limit=POLICY,
            rights=grant,
        )

    assert assess(rights).health_status == HealthStatus.READY_FRESH
    for bad in [
        replace(rights, provider_name="OTHER"),
        replace(rights, authorization_sha256=""),
        replace(rights, allowed_use="FIXTURES_ONLY"),
        replace(rights, registered_at=NOW + timedelta(seconds=1)),
        replace(rights, expires_at=NOW),
    ]:
        assert assess(bad).health_status == HealthStatus.RIGHTS_PENDING


@pytest.mark.parametrize("value", [0, -1, True])
def test_invalid_freshness_policy_rejected(value):
    with pytest.raises(ValueError, match="FRESHNESS_LIMIT"):
        FreshnessPolicy(value, 60)
