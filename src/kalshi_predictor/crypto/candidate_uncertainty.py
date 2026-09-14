"""Candidate applicability boundary; no reviewed candidate-error method exists.

The existing calibration verifier establishes conditional segment mean bias.
It cannot establish the probability error of a selected contract. This contract
records exact decision/evidence bindings and rejects that unsupported conversion.
It never accepts a caller-authored allowance, independent count or verdict.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.crypto.calibration_cost_evidence import verify_uncertainty_evidence

BLOCKER = "CALIBRATION_CANDIDATE_APPLICABILITY_NOT_ESTABLISHED"


def verify_candidate_uncertainty(
    *, decision: dict[str, Any], policy_version: str | None = None,
    dataset: bytes = b"", protocol: bytes = b"", independence_review: bytes = b"",
) -> dict[str, Any]:
    """Replay existing conditional evidence without asserting candidate scope.

    The caller must obtain decision inputs from its original replay. The digest
    binds supplied inputs; it is not an original-receipt or statistical audit.
    All candidate-specific methods remain unsupported, including when the real
    conditional verifier returns paper_support=True. No new registry is created.
    """
    originals = (dataset, protocol, independence_review)
    if any(type(raw) is not bytes or len(raw) > 8_000_000 for raw in originals):
        raise ValueError("CANDIDATE_UNCERTAINTY_ORIGINAL_BOUND")
    if type(decision) is not dict:
        raise ValueError("CANDIDATE_UNCERTAINTY_DECISION_REQUIRED")
    raw = json.dumps(decision, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode()
    if len(raw) > 1_000_000:
        raise ValueError("CANDIDATE_UNCERTAINTY_DECISION_BOUND")
    result: dict[str, Any] = {
        "version": "CANDIDATE_UNCERTAINTY_APPLICABILITY_V1",
        "status": "UNKNOWN", "value": None, "paper_support": False,
        "candidate_applicability": False, "execution_authority": False,
        "candidate_sha256": hashlib.sha256(raw).hexdigest(),
        "policy_version": policy_version,
        "original_sha256": dict(zip(
            ("dataset", "protocol", "independence_review"),
            (hashlib.sha256(value).hexdigest() for value in originals), strict=True)),
        "original_receipts_replayed": False,
        "scope": "CONDITIONAL_MEAN_BIAS_IS_NOT_SELECTED_CANDIDATE_PROBABILITY_ERROR",
        "conditional_evidence_supported": False,
        "blockers": [BLOCKER, "REVIEWED_CANDIDATE_ERROR_METHOD_UNAVAILABLE",
                     "SELECTION_AND_TARGET_SCOPE_NOT_STATISTICALLY_JUSTIFIED"],
    }
    try:
        if any(not isinstance(decision.get(key), str) or not decision[key]
               for key in ("ticker", "event_id", "model_version", "segment")):
            raise ValueError("CANDIDATE_IDENTITY_INCOMPLETE")
        if decision.get("side") not in ("BUY_YES", "BUY_NO"):
            raise ValueError("CANDIDATE_SIDE_INVALID")
        value = decision["selected_probability"]
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("CANDIDATE_PROBABILITY_INVALID")
        probability = Decimal(value)
        if not probability.is_finite() or not 0 <= probability <= 1:
            raise ValueError("CANDIDATE_PROBABILITY_INVALID")
        at = datetime.fromisoformat(decision["decision_at"])
        if at.utcoffset() is None:
            raise ValueError("CANDIDATE_DECISION_CLOCK_REQUIRED")
        component = verify_uncertainty_evidence(
            model_version=decision["model_version"], segment=decision["segment"],
            decision_at=at, policy_version=policy_version, dataset=dataset,
            protocol=protocol, independence_review=independence_review)
        result["conditional_evidence_supported"] = component.paper_support
        result["blockers"].extend(component.blockers)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        result["blockers"].append("CANDIDATE_UNCERTAINTY_REPLAY_REJECTED:" + str(exc))
    return result
