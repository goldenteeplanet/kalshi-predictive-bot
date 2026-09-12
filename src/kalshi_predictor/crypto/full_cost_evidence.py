"""Shared original-evidence assessment; reported costs never grant execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.crypto.account_fee_evidence import FeeAuthorityOriginal, verify_account_fee
from kalshi_predictor.crypto.calibration_cost_evidence import verify_uncertainty_evidence
from kalshi_predictor.crypto.cost_evidence import (
    CostEvidenceResult,
    CostEvidenceStatus,
    OriginalBook,
    observed_one_contract_stress,
)
from kalshi_predictor.overnight_paper.rule_verifier import RuleDocument, verify_settlement_rule


@dataclass(frozen=True)
class FullCostEvidenceAssessment:
    gross_edge: Decimal
    exchange_fee: CostEvidenceResult
    observed_book_stress: CostEvidenceResult
    uncertainty: CostEvidenceResult
    full_net_ev: Decimal | None
    full_net_ev_status: str
    qualification_status: str
    blockers: tuple[str, ...]
    shortfall_to_five_cents: Decimal | None
    clears_net_gate: bool
    execution_authority: bool = False


def assess_full_cost_evidence(
    *, decision: dict[str, Any], selected_probability: Decimal,
    executable_price: Decimal, side: str, books: tuple[OriginalBook, ...] = (),
    account_identity_sha256: str = "", fee_policy_version: str | None = None,
    fee_originals: tuple[FeeAuthorityOriginal, ...] = (),
    calibration_policy_version: str | None = None, calibration_dataset: bytes = b"",
    calibration_protocol: bytes = b"", independence_review: bytes = b"",
    rule_documents: tuple[RuleDocument, ...] = (),
) -> FullCostEvidenceAssessment:
    """Reverify components; do not accept component results or supplied verdicts.

    Probability is for the explicitly selected YES/NO side, not implicitly a YES
    probability. Caller identity/price must match the decision. Missing evidence
    is null. Observed book stress is reported separately and cannot substitute
    for expected execution slippage. Model release/risk remain separate gates.
    """
    if (
        not isinstance(selected_probability, Decimal) or not selected_probability.is_finite()
        or not 0 <= selected_probability <= 1
        or not isinstance(executable_price, Decimal) or not executable_price.is_finite()
        or not 0 < executable_price < 1 or side not in ("YES", "NO")
    ):
        raise ValueError("COST_EXPLICIT_SIDE_PROBABILITY_PRICE_REQUIRED")
    if (
        decision.get("side") != "BUY_" + side
        or Decimal(str(decision["executable_price"])) != executable_price
        or Decimal(str(decision["selected_probability"])) != selected_probability
        or not all(decision.get(k) for k in (
            "ticker", "event_id", "series", "model_version", "segment",
        ))
    ):
        raise ValueError("COST_DECISION_BINDING_MISMATCH")
    at = datetime.fromisoformat(decision["decision_at"])
    if at.utcoffset() is None:
        raise ValueError("COST_AWARE_DECISION_REQUIRED")
    fee = verify_account_fee(
        account_identity_sha256=account_identity_sha256, series=decision["series"],
        event=decision["event_id"], executable_price=executable_price, decision_at=at,
        policy_version=fee_policy_version, originals=fee_originals,
    )
    uncertainty = verify_uncertainty_evidence(
        model_version=decision["model_version"], segment=decision["segment"], decision_at=at,
        policy_version=calibration_policy_version, dataset=calibration_dataset,
        protocol=calibration_protocol, independence_review=independence_review,
    )
    try:
        stress = observed_one_contract_stress(
            ticker=decision["ticker"], side=side, executable_price=executable_price,
            originals=books, decision_at=at,
        )
    except (ValueError, TypeError, KeyError) as exc:
        stress = CostEvidenceResult(
            "slippage", None, "USD_PER_ONE_DOLLAR_PAYOUT", "OBSERVED_BOOK_STRESS",
            "ORIGINAL_BOOK_DEPTH_AND_FULL_RANGE_V2",
            tuple((b.url, b.sha256) for b in books), at, CostEvidenceStatus.UNKNOWN,
            (str(exc),), False,
        )
    rule = verify_settlement_rule(decision=decision, documents=rule_documents)
    blockers: list[str] = []
    if not rule.passed:
        blockers.extend(("RULE_UNCERTIFIED", *rule.blockers))
    if fee.value is None or fee.status != CostEvidenceStatus.CERTIFIED:
        blockers.extend(("FEE_APPLICABILITY_UNKNOWN", *fee.blockers))
    if not stress.paper_support:
        blockers.extend(("EXPECTED_SLIPPAGE_UNKNOWN", *stress.blockers))
    if uncertainty.value is None or not uncertainty.paper_support:
        blockers.extend(("CALIBRATION_BLOCKED", *uncertainty.blockers))
    gross = selected_probability - executable_price
    # The current book method is diagnostic only. A future reviewed execution
    # method must be invoked here before it can supply an applicable cost.
    values = (fee.value, stress.value if stress.paper_support else None, uncertainty.value)
    net = None
    if fee.status == CostEvidenceStatus.CERTIFIED and all(v is not None for v in values):
        net = gross - sum((v for v in values if v is not None), Decimal(0))
    if net is None:
        blockers.append("FULL_NET_EV_UNKNOWN")
    elif net <= Decimal(".05"):
        blockers.append("NET_EV_NOT_STRICTLY_ABOVE_FIVE_CENTS")
    status = (
        "RULE_BLOCKED" if not rule.passed else "COST_BLOCKED" if net is None
        else "CALIBRATION_BLOCKED" if not uncertainty.paper_support
        else "FULL_NET_EV_KNOWN"
    )
    return FullCostEvidenceAssessment(
        gross, fee, stress, uncertainty, net,
        "FULL_NET_EV_UNKNOWN" if net is None else "FULL_NET_EV_KNOWN", status,
        tuple(dict.fromkeys(blockers)),
        None if net is None else max(Decimal(0), Decimal(".05") - net),
        net is not None and net > Decimal(".05"),
    )
