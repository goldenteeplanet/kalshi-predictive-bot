"""Consume existing reviewed original evidence without granting candidate scope.

The existing estimator bounds conditional segment mean bias, not each selected
market's probability error. Exposing its result never promotes a research model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.crypto.account_fee_evidence import FeeAuthorityOriginal
from kalshi_predictor.crypto.cost_evidence import OriginalBook
from kalshi_predictor.crypto.full_cost_evidence import assess_public_paper_cost_evidence


@dataclass(frozen=True)
class CurrentCalibrationOriginals:
    """References and originals only; no caller-supplied reserve or verdict."""

    policy_version: str
    segment: str
    dataset: bytes
    protocol: bytes
    independence_review: bytes

    def byte_count(self) -> int:
        values = (self.dataset, self.protocol, self.independence_review)
        if any(type(raw) is not bytes or len(raw) > 8_000_000 for raw in values):
            raise ValueError("BOUNDED_CALIBRATION_ORIGINALS_REQUIRED")
        if not self.segment or not self.policy_version:
            raise ValueError("CALIBRATION_POLICY_AND_SEGMENT_REQUIRED")
        return sum(map(len, values))


def assess_current_conditional_calibration(
    *,
    originals: CurrentCalibrationOriginals,
    ticker: str,
    event_id: str,
    series: str,
    model_version: str,
    side: str,
    selected_probability: Decimal,
    executable_price: Decimal,
    decision_at: datetime,
    book: OriginalBook,
    fee_originals: tuple[FeeAuthorityOriginal, ...],
) -> dict[str, Any]:
    """Replay canonical cost validators; applicability remains separately blocked.

    Even a supported result remains explicitly conditional. The caller-provided
    segment label cannot establish that a current candidate belongs to a reviewed
    population or that candidate selection preserves its probability guarantee.
    """
    if type(originals) is not CurrentCalibrationOriginals:
        raise ValueError("EXACT_CALIBRATION_ORIGINAL_BUNDLE_REQUIRED")
    originals.byte_count()
    result: dict[str, Any] = {
        "version": "CURRENT_CONDITIONAL_CALIBRATION_V1",
        "status": "UNKNOWN",
        "uncertainty_evidence": None,
        "conditional_uncertainty_value": None,
        "conditional_net_ev": None,
        "candidate_applicability": False,
        "paper_eligible": False,
        "execution_authority": False,
        "scope": "CONDITIONAL_SEGMENT_BIAS_NOT_CANDIDATE_PROBABILITY_ERROR",
        "dataset_replay_scope": "PINNED_FLAT_ROWS_NOT_AUTOMATIC_SOURCE_RECEIPT_REPLAY",
        "scenario_scope_verified": False,
        "holdout_transportability_verified": False,
        "applicability_blocker": "CALIBRATION_CANDIDATE_APPLICABILITY_NOT_ESTABLISHED",
        "blockers": [],
    }
    try:
        assessment = assess_public_paper_cost_evidence(
            public_paper_fee_originals=fee_originals,
            public_paper_assessed_at=decision_at,
            decision={
                "ticker": ticker,
                "event_id": event_id,
                "series": series,
                "model_version": model_version,
                "segment": originals.segment,
                "side": "BUY_" + side,
                "selected_probability": str(selected_probability),
                "executable_price": str(executable_price),
                "decision_at": decision_at.isoformat(),
            },
            selected_probability=selected_probability,
            executable_price=executable_price,
            side=side,
            books=(book,),
            calibration_policy_version=originals.policy_version,
            calibration_dataset=originals.dataset,
            calibration_protocol=originals.protocol,
            independence_review=originals.independence_review,
        )
        component = assessment["uncertainty"]
        result["uncertainty_evidence"] = json.loads(json.dumps(component, default=str))
        result["blockers"] = list(assessment["blockers"])
        if component["paper_support"] and component["value"] is not None:
            result["status"] = "CONDITIONALLY_SUPPORTED_NOT_CANDIDATE_CERTIFIED"
            result["conditional_uncertainty_value"] = str(component["value"])
            value = assessment["full_net_ev"]
            result["conditional_net_ev"] = str(value) if value is not None else None
        result["blockers"].append(result["applicability_blocker"])
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        result["blockers"] = [
            "CALIBRATION_ORIGINAL_REPLAY_REJECTED:" + str(exc),
            result["applicability_blocker"],
        ]
    return result
