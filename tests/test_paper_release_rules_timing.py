from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.overnight_paper.rule_verifier import (
    CertifiedRulePolicy,
    RuleDocument,
    select_exact_observation,
    verify_settlement_rule,
)
from kalshi_predictor.overnight_paper.timing import (
    canonical_market_times,
    verify_settlement_horizon,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def fixture():
    document = RuleDocument("https://kalshi.com/fixture-rule", b"synthetic test document")
    policy = CertifiedRulePolicy(
        ticker="TEST-E-M",
        event_id="TEST-E",
        series="TEST",
        provider="fixture",
        source_identity="fixture-station",
        observation_time="2026-09-08T01:00:00Z",
        selection="exact timestamp",
        conversion="identity",
        precision="original decimal",
        rounding="none",
        finality="final by explicit deadline; no review extension",
        effective_from="2026-09-01T00:00:00Z",
        effective_to="2026-10-01T00:00:00Z",
        documents=((document.url, document.sha256),),
        amendments=(),
        methodology="exact-timestamp-decimal-v1",
        expected_settlement_seconds=60,
        final_settlement_seconds=120,
        review_extension_seconds=0,
    )
    decision = {
        "ticker": policy.ticker,
        "event_id": policy.event_id,
        "series": policy.series,
        "rule_version": policy.version,
        "decision_at": NOW.isoformat(),
        "observation_time": policy.observation_time,
        "settlement_rule": asdict(policy),
        "market_open_time": "2026-09-07T00:00:00Z",
        "market_close_time": policy.observation_time,
        "expected_settlement_time": "2026-09-08T01:01:00Z",
        "settlement_deadline": "2026-09-08T01:02:00Z",
        "final_settlement_time": None,
    }
    decision["settlement_rule"]["amendments"] = []
    return decision, policy, document


def test_only_explicit_registry_dependency_can_certify_fixture():
    decision, policy, document = fixture()
    assert not verify_settlement_rule(decision=decision, documents=(document,)).passed
    verified = verify_settlement_rule(decision=decision, documents=(document,), registry=(policy,))
    assert verified.passed
    assert verify_settlement_horizon(decision=decision, rule=verified, now=NOW).passed


@pytest.mark.parametrize(
    "field,value",
    [
        ("ticker", "OTHER"),
        ("event_id", "OTHER"),
        ("series", "OTHER"),
        ("rule_version", "PASS"),
        ("observation_time", "2026-10-01T00:00:00Z"),
    ],
)
def test_mismatched_or_stale_rule_fails(field, value):
    decision, policy, document = fixture()
    decision[field] = value
    assert not verify_settlement_rule(
        decision=decision, documents=(document,), registry=(policy,)
    ).passed


@pytest.mark.parametrize(
    "field",
    [
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
        "amendments",
    ],
)
def test_every_settlement_semantic_is_bound(field):
    decision, policy, document = fixture()
    del decision["settlement_rule"][field]
    assert not verify_settlement_rule(
        decision=decision, documents=(document,), registry=(policy,)
    ).passed


def test_document_hash_and_ambiguity_checked():
    decision, policy, document = fixture()
    tampered = replace(document, payload=b"PASS")
    assert not verify_settlement_rule(
        decision=decision, documents=(tampered,), registry=(policy,)
    ).passed
    assert not verify_settlement_rule(
        decision=decision, documents=(document,), registry=(policy, policy)
    ).passed


def test_expiration_never_establishes_settlement():
    decision, policy, document = fixture()
    verified = verify_settlement_rule(decision=decision, documents=(document,), registry=(policy,))
    decision.pop("settlement_deadline")
    decision["expiration_time"] = "2026-09-08T01:02:00Z"
    decision["latest_expiration_time"] = "2026-09-08T01:02:00Z"
    assert canonical_market_times(decision)["settlement_deadline"] is None
    assert not verify_settlement_horizon(decision=decision, rule=verified, now=NOW).passed


@pytest.mark.parametrize("seconds,passes", [(72 * 3600 - 3600, True), (72 * 3600 - 3599, False)])
def test_exact_72_hour_boundary(seconds, passes):
    decision, policy, document = fixture()
    policy = replace(policy, final_settlement_seconds=seconds)
    decision["rule_version"] = policy.version
    decision["settlement_deadline"] = (NOW + timedelta(hours=1, seconds=seconds)).isoformat()
    verified = verify_settlement_rule(decision=decision, documents=(document,), registry=(policy,))
    assert verified.passed
    assert verify_settlement_horizon(decision=decision, rule=verified, now=NOW).passed is passes


def test_review_exception_included_and_unknown_rejected():
    decision, policy, document = fixture()
    policy = replace(policy, review_extension_seconds=None)
    decision["rule_version"] = policy.version
    assert not verify_settlement_rule(
        decision=decision, documents=(document,), registry=(policy,)
    ).passed
    policy = replace(policy, review_extension_seconds=72 * 3600)
    decision["rule_version"] = policy.version
    decision["settlement_deadline"] = (NOW + timedelta(hours=73, seconds=120)).isoformat()
    verified = verify_settlement_rule(decision=decision, documents=(document,), registry=(policy,))
    assert verified.passed
    assert not verify_settlement_horizon(decision=decision, rule=verified, now=NOW).passed


def test_exact_selection_never_interpolates_or_accepts_duplicate():
    assert (
        select_exact_observation(((NOW.isoformat(), "71.25"),), observation_time=NOW.isoformat())
        == "71.25"
    )
    with pytest.raises(ValueError):
        select_exact_observation((), observation_time=NOW.isoformat())
    with pytest.raises(ValueError):
        select_exact_observation(
            ((NOW.isoformat(), "71"), (NOW.isoformat(), "72")), observation_time=NOW.isoformat()
        )


@pytest.mark.parametrize("actual", ["2026-09-08T01:02:00Z", "2026-09-07T01:02:00Z"])
def test_actual_settlement_timestamp_forbidden_before_entry(actual):
    decision, policy, document = fixture()
    verified = verify_settlement_rule(decision=decision, documents=(document,), registry=(policy,))
    decision["final_settlement_time"] = actual
    result = verify_settlement_horizon(decision=decision, rule=verified, now=NOW)
    assert not result.passed
    assert result.blockers == ("ACTUAL_SETTLEMENT_TIME_PRESENT_BEFORE_ENTRY",)


def test_assets_document_supported_without_certifying_unknown_policy():
    decision, policy, document = fixture()
    document = replace(document, url="https://assets.kalshi.com/fixture-rule.pdf")
    policy = replace(policy, documents=((document.url, document.sha256),))
    decision["rule_version"] = policy.version
    assert verify_settlement_rule(
        decision=decision, documents=(document,), registry=(policy,)
    ).passed
    assert not verify_settlement_rule(decision=decision, documents=(document,)).passed


def test_result_exposes_deadline_without_asserting_actual_settlement():
    decision, policy, document = fixture()
    verified = verify_settlement_rule(decision=decision, documents=(document,), registry=(policy,))
    result = verify_settlement_horizon(decision=decision, rule=verified, now=NOW)
    assert result.passed
    assert result.settlement_deadline == datetime(2026, 9, 8, 1, 2, tzinfo=UTC)
    assert canonical_market_times(decision)["final_settlement_time"] is None
