"""Public-schedule paper economics, distinct from an actual account invoice.

The operator-selected cent model is a conservative paper assumption. The July
2026 PDF formula and examples disagree on rounding, so it is not represented as
an account-specific exact fee. No absence-of-account-attestation blocker applies.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Any

from kalshi_predictor.crypto.account_fee_evidence import FeeAuthorityOriginal
from kalshi_predictor.crypto.cost_evidence import OriginalBook, _levels

POLICY = "PUBLIC_GENERAL_TAKER_CENT_PAPER_V1"
SCHEDULE_URL = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
FACTS_HASH = "d6cea021d3d2a179e8ae76161b3a3bb4d0fe4deaba534c552a90b3dc4597e9c3"
SERIES_HASHES = {
    "KXBTC": "14724d5d6f0edc99f26176cd2deddbdc77873f29b4cbfb1dd88cc5e41e10f968",
    "KXETH": "28dd6ace9968a09c23e6fba1a1c49a82edb46f3313237767d1015a5fde5a46ab",
    "KXSOLE": "4f242773eb811380db55ea44424f1fd03d324757baea88b0d7bc0c9b297495a4",
    "KXXRP": "8d32f3f0f9f7e4f3431cde7c8ea9118d59588f588745387778ab6ddfd4bb6cce",
    "KXDOGE": "493d6716921550544bb625f460dde2cea245bae69054e6f3a93885ba118644b9",
}
EMPTY_CHANGES_HASH = "6780c8eb7edbb5e1ca3b15166f17cef054befacb2cc8f4bc479c54074a9a2c18"


def replayed_fee_supports_paper(component: dict[str, Any]) -> bool:
    """Use only AFTER original cost replay, never as a raw evidence validator."""
    return component.get('status') == 'CERTIFIED' or (
        component.get('status') == 'ESTIMATED_WITH_SUPPORT'
        and component.get('method') == POLICY
        and component.get('scope') == 'LOCAL_PAPER_MODEL_NOT_ACCOUNT_INVOICE'
        and component.get('paper_support') is True
        and component.get('exact_account_fee_certified') is False
    )


def general_taker_cent_fee(price: Decimal, quantity: int = 1) -> Decimal:
    """Exact requested whole-cent model; quantity is explicitly one contract."""
    if (not isinstance(price, Decimal) or not price.is_finite()
            or not 0 < price < 1 or type(quantity) is not int or quantity != 1):
        raise ValueError("ONE_CONTRACT_FINITE_FIXED_POINT_PRICE_REQUIRED")
    exponent = price.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -6:
        raise ValueError("ONE_CONTRACT_FINITE_FIXED_POINT_PRICE_REQUIRED")
    with localcontext() as context:
        context.prec = 40
        return (Decimal(".07") * price * (1-price)).quantize(
            Decimal(".01"), rounding=ROUND_CEILING,
        )


def public_paper_fee(
    *, series: str, price: Decimal, originals: tuple[FeeAuthorityOriginal, ...],
    assessed_at: datetime, policy: str = POLICY,
) -> dict[str, Any]:
    """Replay a reviewed public-document snapshot, without inventing membership.

    Evidence is pinned to the documented review, not accepted from caller labels.
    Assessment time is separate from the frozen decision time. Current policy
    may be applied retrospectively for diagnostics, never backdated eligibility.
    """
    if assessed_at.utcoffset() is None or policy != POLICY:
        raise ValueError("PUBLIC_PAPER_POLICY_AND_AWARE_ASSESSMENT_REQUIRED")
    value = general_taker_cent_fee(price)
    sources = tuple((o.url, hashlib.sha256(o.payload).hexdigest()) for o in originals)
    result: dict[str, Any] = {
        "component": "exchange_fee", "value": None,
        "unit": "USD_PER_ONE_DOLLAR_PAYOUT", "method": POLICY, "version": POLICY,
        "timestamp": assessed_at.isoformat(), "status": "UNKNOWN",
        "paper_support": False, "evidence_sources": sources,
        "blockers": ["PUBLIC_FEE_ORIGINALS_NOT_REVIEWED"],
        "applicability": "FEE_UNKNOWN", "execution_authority": False,
        "fee_formula_id": "CEIL_CENT_0.07_C_P_1_MINUS_P",
        "fee_schedule_version": "2026-07-07",
        "price": str(price), "quantity": 1, "calculated_fee": None,
        "scope": "LOCAL_PAPER_MODEL_NOT_ACCOUNT_INVOICE",
    }
    if series not in SERIES_HASHES or len(originals) != 3:
        return result
    if price != price.quantize(Decimal('.01')):
        result['blockers'] = ['PUBLIC_PAPER_CENT_MODEL_SUBCENT_PRICE_UNREVIEWED']
        return result
    expected = {
        SCHEDULE_URL: FACTS_HASH,
        f"https://external-api.kalshi.com/trade-api/v2/series/{series}": SERIES_HASHES[series],
        "https://external-api.kalshi.com/trade-api/v2/series/fee_changes"
        f"?series_ticker={series}&show_historical=true": EMPTY_CHANGES_HASH,
    }
    if len(dict(sources)) != 3 or dict(sources) != expected:
        return result
    for original in originals:
        if (len(original.payload) > 2_000_000 or original.received_at.utcoffset() is None
                or not 0 <= (assessed_at-original.received_at).total_seconds() <= 86400):
            return result
    metadata = json.loads(next(o.payload for o in originals if o.url.endswith('/'+series)))
    if (metadata['series']['ticker'] != series or metadata['series']['fee_type'] != 'quadratic'
            or Decimal(str(metadata['series']['fee_multiplier'])) != 1):
        result.update(applicability="FEE_CONFLICT", blockers=["PUBLIC_SERIES_FEE_CONFLICT"])
        return result
    result.update(
        value=str(value), calculated_fee=str(value), status="ESTIMATED_WITH_SUPPORT",
        evidence_status="SUPPORTED_ESTIMATE", paper_support=True,
        applicability="GENERAL_PUBLIC_FEE_APPLIES", blockers=[],
        rounding_status="FEE_CONFLICT_FORMULA_VERSUS_EXAMPLES",
        rounding_policy="OPERATOR_SELECTED_CONSERVATIVE_CENT_MODEL",
        exact_account_fee_certified=False,
        source_representation="FEE_FACTS_EXTRACTION_AND_ORIGINAL_SERIES_RESPONSES",
    )
    return result


def snapshot_one_contract_impact(
    *, ticker: str, side: str, price: Decimal,
    originals: tuple[OriginalBook, ...], decision_at: datetime,
) -> dict[str, Any]:
    """One-contract VWAP minus the selected ask; no spread/quote-range surcharge.

    This conditional snapshot fill is not a claim of a realized exchange fill.
    Adverse selection and latency belong to a separate uncertainty estimate.
    """
    if (decision_at.utcoffset() is None or side not in ('YES', 'NO')
            or not isinstance(price, Decimal) or not price.is_finite()
            or not 0 < price < 1 or not 1 <= len(originals) <= 100):
        raise ValueError("SNAPSHOT_FILL_BOUND_INPUTS_REQUIRED")
    if any(o.received_at.utcoffset() is None for o in originals):
        raise ValueError("SNAPSHOT_AWARE_RECEIPTS_REQUIRED")
    times = [o.received_at for o in originals]
    if any(a >= b for a, b in zip(times, times[1:], strict=False)):
        raise ValueError("SNAPSHOT_RECEIPT_ORDER_INVALID")
    eligible = [o for o in originals if o.received_at <= decision_at]
    result: dict[str, Any] = {
        "component": "execution_price_impact", "value": None,
        "unit": "USD_PER_ONE_DOLLAR_PAYOUT", "method": "ONE_CONTRACT_SNAPSHOT_VWAP",
        "version": "SNAPSHOT_FILL_V1", "timestamp": decision_at.isoformat(),
        "status": "UNKNOWN", "paper_support": False, "evidence_sources": [],
        "blockers": ["BOOK_FILL_PRICE_UNKNOWN"], "fill_status": "BOOK_FILL_PRICE_UNKNOWN",
        "scope": "CONDITIONAL_SIMULATED_FILL_AT_CAPTURED_BOOK",
        "adverse_selection_status": "ADVERSE_SELECTION_UNCERTAINTY",
        "adverse_selection_cost": None, "latency_cost": None,
        "execution_authority": False,
    }
    if not eligible or (decision_at-eligible[-1].received_at).total_seconds() > 60:
        return result
    original = eligible[-1]
    result['evidence_sources'] = [(original.url, original.sha256)]
    try:
        book = _levels(original, ticker, require_two_sided=False)
    except (ValueError, KeyError, TypeError) as exc:
        result['blockers'].append(str(exc))
        return result
    asks = [(1-p, q) for p, q in book['no' if side == 'YES' else 'yes']]
    if not asks or asks[0][0] != price:
        result['blockers'].append('SNAPSHOT_EXECUTABLE_PRICE_MISMATCH')
        return result
    left = Decimal(1)
    cost = Decimal(0)
    for level_price, size in asks:
        fill = min(left, size)
        cost += fill*level_price
        left -= fill
        if left == 0:
            break
    if left:
        result['blockers'].append('SNAPSHOT_INSUFFICIENT_ONE_CONTRACT_DEPTH')
        return result
    result.update(
        value=str(cost-price), fill_price=str(cost), best_ask_quantity=str(asks[0][1]),
        status="CERTIFIED", evidence_status="CERTIFIED", paper_support=True,
        blockers=[], fill_status="BOOK_FILL_PRICE_KNOWN",
        timestamp=original.received_at.isoformat(),
    )
    return result
