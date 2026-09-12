"""Synthetic policy registry fixtures are not production authority documents."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.crypto import account_fee_evidence as fees
from kalshi_predictor.crypto.cost_evidence import CostEvidenceStatus

AT = datetime(2026, 9, 12, 12, tzinfo=UTC)
ORIGINAL = fees.FeeAuthorityOriginal(
    "https://kalshi.com/docs/test-only-account-policy", b"SYNTHETIC TEST ONLY", AT,
)
POLICY = fees.ReviewedAccountFeePolicy(
    "a" * 64, "TEST_ONLY", "KXSOLE", "KXSOLE-TEST", AT, AT + timedelta(hours=1), AT,
    Decimal(".07"), Decimal(1), Decimal(".01"), Decimal(".003"), Decimal(".001"),
    ((ORIGINAL.url, ORIGINAL.sha256),),
)


def verify(policy=POLICY, **kwargs):
    args = dict(
        account_identity_sha256="a" * 64, series="KXSOLE", event="KXSOLE-TEST",
        executable_price=Decimal(".5"), decision_at=AT,
        policy_version=policy.version, originals=(ORIGINAL,),
    )
    return fees.verify_account_fee(**(args | kwargs))


def test_unregistered_caller_policy_and_original_hash_are_insufficient():
    assert fees.REVIEWED_ACCOUNT_FEE_POLICIES == ()
    result = verify()
    assert result.status == CostEvidenceStatus.UNKNOWN
    assert result.value is None
    assert not result.paper_support


def test_reviewed_fixture_recomputes_fee_and_all_additional_charges(monkeypatch):
    monkeypatch.setattr(fees, "REVIEWED_ACCOUNT_FEE_POLICIES", (POLICY,))
    result = verify()
    assert result.value == Decimal(".024")
    assert result.status == CostEvidenceStatus.CERTIFIED
    assert result.paper_support


@pytest.mark.parametrize("changes", [
    {"account_identity_sha256": "b" * 64}, {"series": "KXBTC"}, {"event": "KXSOLE-OTHER"},
    {"decision_at": AT + timedelta(hours=1)},
    {"originals": (replace(ORIGINAL, payload=b"altered"),)},
    {"originals": (replace(ORIGINAL, received_at=AT + timedelta(seconds=1)),)},
])
def test_scope_original_or_clock_mismatch_stays_unknown(monkeypatch, changes):
    monkeypatch.setattr(fees, "REVIEWED_ACCOUNT_FEE_POLICIES", (POLICY,))
    result = verify(**changes)
    assert result.value is None
    assert not result.paper_support


def test_later_review_explains_history_without_backdating_admission(monkeypatch):
    policy = replace(POLICY, reviewed_at=AT + timedelta(minutes=1))
    monkeypatch.setattr(fees, "REVIEWED_ACCOUNT_FEE_POLICIES", (policy,))
    result = verify(policy)
    assert result.value == Decimal(".024")
    assert not result.paper_support
    assert result.blockers == ("FEE_REVIEW_POSTDECISION_HISTORICAL_ONLY",)


def test_subcent_precision_is_part_of_reviewed_policy(monkeypatch):
    policy = replace(POLICY, balance_precision=Decimal(".0001"))
    monkeypatch.setattr(fees, "REVIEWED_ACCOUNT_FEE_POLICIES", (policy,))
    assert verify(policy).value == Decimal(".0215")
