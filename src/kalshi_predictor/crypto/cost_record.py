"""Durable original-evidence replay; stored assessment verdicts are never trusted."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.crypto.account_fee_evidence import FeeAuthorityOriginal
from kalshi_predictor.crypto.candidate_uncertainty import verify_candidate_uncertainty
from kalshi_predictor.crypto.cost_evidence import OriginalBook
from kalshi_predictor.crypto.full_cost_evidence import (
    assess_full_cost_evidence,
    assess_public_paper_cost_evidence,
)
from kalshi_predictor.overnight_paper.rule_verifier import RuleDocument

KIND = "ORIGINAL_FULL_COST_ASSESSMENT_V1"
PUBLIC_PAPER_KIND = "DOCUMENTED_PUBLIC_PAPER_COST_ASSESSMENT_V2"


def cost_decision_from_qualification(inputs: dict[str, Any]) -> dict[str, Any]:
    """Derive cost scope without mutating the original qualification decision.

    Forecast probability follows the engine's YES convention. Cost probability
    is complemented exactly once for BUY_NO. Model identity uses the bound
    artifact digest; an undeclared segment remains explicitly undeclared.
    """
    side = inputs.get("side")
    if side not in ("BUY_YES", "BUY_NO"):
        raise ValueError("COST_QUALIFICATION_BUY_SIDE_REQUIRED")
    probability = Decimal(str(inputs["forecast_probability"]))
    if not probability.is_finite() or not 0 <= probability <= 1:
        raise ValueError("COST_QUALIFICATION_PROBABILITY_INVALID")
    model = inputs["model_artifact_sha256"]
    if len(model) != 64 or any(c not in "0123456789abcdef" for c in model):
        raise ValueError("COST_BOUND_MODEL_ARTIFACT_REQUIRED")
    return inputs | {
        "selected_probability": str(probability if side == "BUY_YES" else 1-probability),
        "model_version": model,
        "segment": inputs.get("calibration_segment") or "UNDECLARED",
    }


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str,
                      allow_nan=False).encode()


def _original(raw: bytes) -> dict[str, str]:
    return {"payload_hex": raw.hex(), "sha256": hashlib.sha256(raw).hexdigest()}


def _restore(original: dict[str, Any]) -> bytes:
    encoded = original["payload_hex"]
    if not isinstance(encoded, str) or len(encoded) > 16_000_000:
        raise ValueError("COST_ORIGINAL_SIZE_LIMIT")
    raw = bytes.fromhex(encoded)
    if hashlib.sha256(raw).hexdigest() != original["sha256"]:
        raise ValueError("COST_ORIGINAL_HASH_MISMATCH")
    return raw


def build_cost_record(**request: Any) -> dict[str, Any]:
    """Store originals alongside a recomputed diagnostic, using only JSON types."""
    public = 'public_paper_fee_originals' in request
    assessment = (assess_public_paper_cost_evidence(**request) if public
                  else asdict(assess_full_cost_evidence(**request)))
    payload = {
        "decision": request["decision"], "side": request["side"],
        "selected_probability": str(request["selected_probability"]),
        "executable_price": str(request["executable_price"]),
        "books": [dict(url=b.url, received_at=b.received_at.isoformat(), **_original(b.payload))
                  for b in request.get("books", ())],
        "account_identity_sha256": request.get("account_identity_sha256", ""),
        "fee_policy_version": request.get("fee_policy_version"),
        "fee_originals": [dict(url=o.url, received_at=o.received_at.isoformat(),
                               **_original(o.payload)) for o in request.get("fee_originals", ())],
        "calibration_policy_version": request.get("calibration_policy_version"),
        "calibration_dataset": _original(request.get("calibration_dataset", b"")),
        "calibration_protocol": _original(request.get("calibration_protocol", b"")),
        "independence_review": _original(request.get("independence_review", b"")),
        "rule_documents": [dict(url=d.url, **_original(d.payload))
                           for d in request.get("rule_documents", ())],
    }
    if public:
        payload['public_paper_fee_originals'] = [
            dict(url=o.url, received_at=o.received_at.isoformat(), **_original(o.payload))
            for o in request['public_paper_fee_originals']
        ]
        payload['public_paper_assessed_at'] = request['public_paper_assessed_at'].isoformat()
    return json.loads(_json({"kind": PUBLIC_PAPER_KIND if public else KIND,
                             "request": payload, "assessment": assessment}))


def replay_cost_record(
    record: dict[str, Any], *, expected_decision: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild at the original decision clock, never refresh historical eligibility.

    Caller must supply the independently bound decision. Registry changes may
    invalidate historical replay; they must not silently rewrite old records.
    """
    if len(_json(record)) > 80_000_000 or record.get("kind") not in (KIND, PUBLIC_PAPER_KIND):
        raise ValueError("COST_RECORD_SCHEMA_OR_SIZE_INVALID")
    request = record["request"]
    if _json(request["decision"]) != _json(expected_decision):
        raise ValueError("COST_RECORD_DECISION_MISMATCH")
    if len(request["books"]) > 100 or len(request["fee_originals"]) > 12:
        raise ValueError("COST_RECORD_ORIGINAL_COUNT_LIMIT")
    extra: dict[str, Any] = {}
    if record['kind'] == PUBLIC_PAPER_KIND:
        if len(request['public_paper_fee_originals']) != 3:
            raise ValueError('PUBLIC_PAPER_THREE_FEE_ORIGINALS_REQUIRED')
        extra = dict(
            public_paper_assessed_at=datetime.fromisoformat(request['public_paper_assessed_at']),
            public_paper_fee_originals=tuple(FeeAuthorityOriginal(
                o['url'], _restore(o), datetime.fromisoformat(o['received_at']),
            ) for o in request['public_paper_fee_originals']),
        )
    rebuilt = build_cost_record(
        decision=request["decision"], side=request["side"],
        selected_probability=Decimal(request["selected_probability"]),
        executable_price=Decimal(request["executable_price"]),
        books=tuple(OriginalBook(o["url"], _restore(o), datetime.fromisoformat(o["received_at"]))
                    for o in request["books"]),
        account_identity_sha256=request["account_identity_sha256"],
        fee_policy_version=request["fee_policy_version"],
        fee_originals=tuple(FeeAuthorityOriginal(
            o["url"], _restore(o), datetime.fromisoformat(o["received_at"]),
        ) for o in request["fee_originals"]),
        calibration_policy_version=request["calibration_policy_version"],
        calibration_dataset=_restore(request["calibration_dataset"]),
        calibration_protocol=_restore(request["calibration_protocol"]),
        independence_review=_restore(request["independence_review"]),
        rule_documents=tuple(
            RuleDocument(o["url"], _restore(o)) for o in request["rule_documents"]
        ),
        **extra,
    )
    if _json(rebuilt) != _json(record):
        raise ValueError("COST_RECORD_RECOMPUTATION_MISMATCH")
    return rebuilt["assessment"]


def replay_candidate_cost_record(
    record: dict[str, Any], *, expected_decision: dict[str, Any],
) -> dict[str, Any]:
    """Admission-facing replay separates conditional costs from candidate support.

    Keep historical cost record bytes and their diagnostic replay unchanged.
    Guarded assembly must use this boundary, not conditional paper_support flags.
    """
    assessment = replay_cost_record(record, expected_decision=expected_decision)
    request = record["request"]
    applicability = verify_candidate_uncertainty(
        decision=expected_decision, policy_version=request["calibration_policy_version"],
        dataset=_restore(request["calibration_dataset"]),
        protocol=_restore(request["calibration_protocol"]),
        independence_review=_restore(request["independence_review"]),
    )
    # No current reviewed method establishes candidate probability-error scope.
    # Do not reinterpret conditional diagnostic numbers as canonical full-net EV.
    return assessment | {
        "conditional_assessment": assessment,
        "candidate_applicability": applicability,
        "uncertainty": assessment["uncertainty"] | {
            "value": None, "status": "UNKNOWN", "paper_support": False,
            "blockers": applicability["blockers"],
        },
        "full_net_ev": None, "full_net_ev_status": "FULL_NET_EV_UNKNOWN",
        "clears_net_gate": False,
        "blockers": list(dict.fromkeys(assessment["blockers"] + applicability["blockers"])),
    }
