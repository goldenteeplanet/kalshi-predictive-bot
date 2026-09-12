"""Account-specific fee applicability from code-reviewed original documents.

The registry is deliberately empty until account/series authority is reviewed.
Candidate input cannot supply a policy, rate, amount, or certification verdict.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from kalshi_predictor.crypto.cost_evidence import CostEvidenceResult, CostEvidenceStatus
from kalshi_predictor.crypto.research_costs import single_buy_fee


@dataclass(frozen=True)
class FeeAuthorityOriginal:
    url: str
    payload: bytes
    received_at: datetime

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class ReviewedAccountFeePolicy:
    """Review must substantiate every field, including explicit zero add-ons.

    Originals include account-class authority, general schedule, rounding,
    series/event overrides and broker/settlement charges. Merely copying public
    quadratic metadata is insufficient to add an entry to the code registry.
    Scope is exact; no wildcard account, series, or effective-date fallback.
    """

    account_identity_sha256: str
    account_class: str
    series: str
    event: str
    effective_from: datetime
    effective_to: datetime
    reviewed_at: datetime
    rate: Decimal
    multiplier: Decimal
    balance_precision: Decimal
    additional_per_contract: Decimal
    settlement_per_contract: Decimal
    documents: tuple[tuple[str, str], ...]
    method: str = "ONE_WHOLE_CONTRACT_SINGLE_BUY_ZERO_ACCUMULATOR_V1"

    @property
    def version(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


# No current account/series fee attestation has been independently established.
REVIEWED_ACCOUNT_FEE_POLICIES: tuple[ReviewedAccountFeePolicy, ...] = ()


def verify_account_fee(
    *, account_identity_sha256: str, series: str, event: str,
    executable_price: Decimal, decision_at: datetime, policy_version: str | None,
    originals: tuple[FeeAuthorityOriginal, ...],
) -> CostEvidenceResult:
    """Recompute one-contract costs only after exact reviewed authority binding.

    A review acquired after the decision can explain historical costs but cannot
    support that historical admission. The result grants no order authority.
    """
    if decision_at.utcoffset() is None:
        raise ValueError("FEE_AWARE_DECISION_REQUIRED")
    if (
        not isinstance(executable_price, Decimal) or not executable_price.is_finite()
        or not 0 < executable_price < 1
    ):
        raise ValueError("FEE_OPEN_UNIT_PRICE_REQUIRED")
    if len(originals) > 12 or any(len(o.payload) > 3_000_000 for o in originals):
        raise ValueError("FEE_BOUNDED_ORIGINALS_REQUIRED")
    matches = [p for p in REVIEWED_ACCOUNT_FEE_POLICIES if p.version == policy_version]
    sources = tuple((o.url, o.sha256) for o in originals)

    def unknown(reason: str) -> CostEvidenceResult:
        return CostEvidenceResult(
            "exchange_fee", None, "USD_PER_ONE_DOLLAR_PAYOUT", "ACCOUNT_FEE_APPLICABILITY",
            "REVIEWED_ACCOUNT_FEE_V1", sources, decision_at,
            CostEvidenceStatus.UNKNOWN, (reason,), False,
        )

    if len(matches) != 1:
        return unknown("FEE_ACCOUNT_APPLICABILITY_NOT_REVIEWED")
    policy = matches[0]
    if (
        len(account_identity_sha256) != 64
        or any(c not in "0123456789abcdef" for c in account_identity_sha256)
        or not policy.account_class or not series or not event
        or (account_identity_sha256, series, event)
        != (policy.account_identity_sha256, policy.series, policy.event)
    ):
        return unknown("FEE_ACCOUNT_SERIES_EVENT_SCOPE_MISMATCH")
    if any(t.utcoffset() is None for t in (
        policy.effective_from, policy.effective_to, policy.reviewed_at,
    )) or not policy.effective_from <= decision_at < policy.effective_to:
        return unknown("FEE_POLICY_NOT_EFFECTIVE")
    if (
        not 1 <= len(originals) <= 12 or sources != policy.documents
        or len(set(sources)) != len(sources)
    ):
        return unknown("FEE_REVIEWED_ORIGINALS_MISMATCH")
    for original in originals:
        url = urlsplit(original.url)
        if (
            not 0 < len(original.payload) <= 3_000_000
            or original.received_at.utcoffset() is None
            or original.received_at > policy.reviewed_at
            or url.scheme != "https" or url.username or url.password
            or url.hostname not in {
                "kalshi.com", "help.kalshi.com", "docs.kalshi.com",
                "assets.kalshi.com", "kalshi-public-docs.s3.amazonaws.com",
            }
        ):
            return unknown("FEE_AUTHORITY_OR_REVIEW_CLOCK_INVALID")
    if policy.method != "ONE_WHOLE_CONTRACT_SINGLE_BUY_ZERO_ACCUMULATOR_V1":
        return unknown("FEE_METHOD_NOT_IMPLEMENTED")
    for value in (policy.additional_per_contract, policy.settlement_per_contract):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            return unknown("FEE_ADDITIONAL_CHARGES_NOT_ESTABLISHED")
    fees = single_buy_fee(
        price=executable_price, multiplier=policy.multiplier, rate=policy.rate,
        balance_precision=policy.balance_precision,
    )
    before = policy.reviewed_at <= decision_at
    return CostEvidenceResult(
        "exchange_fee", fees["fee_cost"] + policy.additional_per_contract
        + policy.settlement_per_contract,
        "USD_PER_ONE_DOLLAR_PAYOUT", policy.method, policy.version, sources,
        policy.reviewed_at, CostEvidenceStatus.CERTIFIED,
        () if before else ("FEE_REVIEW_POSTDECISION_HISTORICAL_ONLY",), before,
    )
