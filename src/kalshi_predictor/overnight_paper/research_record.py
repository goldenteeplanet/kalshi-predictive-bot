"""Conservative research view of coordinator qualification checkpoints.

New cost records replay original evidence through the shared assessment.
Legacy records retain their explicit missing-validator status. Numeric
qualification arithmetic alone cannot certify fees or uncertainty.
These records never enter overnight_shadow or grant activation authority.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from kalshi_predictor.crypto.cost_record import (
    cost_decision_from_qualification,
    replay_cost_record,
)
from kalshi_predictor.overnight_paper.qualification import GATE_NAMES, decision_fingerprint
from kalshi_predictor.overnight_paper.store import digest

KIND = "COORDINATOR_RESEARCH_ASSESSMENT_V1"
PREFIX = "release-research-v1:"


def research_record(qualification_checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Derive historical blocker status, preserving the exact qualification hash.

    RULE_UNCERTIFIED takes precedence over BOOK_INVALID, then COST_UNKNOWN.
    All simultaneous blockers remain available. Positive/negative full-net
    classifications are intentionally unavailable without cost certification.
    """
    qualification = qualification_checkpoint["qualification"]
    inputs = qualification_checkpoint["decision_inputs"]
    gates = qualification["gates"]
    if (
        qualification_checkpoint.get("kind") != "PAPER_RELEASE_QUALIFICATION"
        or qualification["decision_id"] != decision_fingerprint(inputs)
        or [item[0] for item in gates] != list(GATE_NAMES)
        or any(type(item[1]) is not bool for item in gates)
    ):
        raise ValueError("RESEARCH_QUALIFICATION_BINDING_INVALID")
    passed = dict(gates)
    blockers = []
    if not passed["CERTIFIED_SETTLEMENT_RULE"]:
        blockers.append("RULE_UNCERTIFIED")
    if not passed["VALID_EXECUTABLE_BOOK"]:
        blockers.append("BOOK_INVALID")
    blockers.append("COST_UNKNOWN")
    record: dict[str, Any] = {
        "kind": KIND,
        "decision_id": qualification["decision_id"],
        "qualification_checkpoint_id": "release-qualification:" + qualification["decision_id"],
        "qualification_checkpoint_sha256": digest(qualification_checkpoint),
        "decision_at": inputs.get("decision_at"),
        "ticker": inputs.get("ticker"),
        "event_id": inputs.get("event_id"),
        "status": blockers[0],
        "research_blockers": blockers,
        "qualification_blockers": qualification["blockers"],
        "provisional_qualification_net_ev": qualification["net_ev"],
        "full_net_ev": None,
        "full_net_ev_status": "FULL_NET_EV_UNKNOWN",
        "cost_evidence_status": "FULL_COST_VALIDATOR_NOT_INTEGRATED",
        "evidence_role": "HISTORICAL_RESEARCH_NOT_CURRENT_ELIGIBILITY",
        "execution_authority": False,
    }
    cost_record = qualification_checkpoint.get("shadow_payload", {}).get("cost_record")
    if cost_record is not None:
        costs = replay_cost_record(
            cost_record, expected_decision=cost_decision_from_qualification(inputs),
        )
        record.update(
            cost_evidence_status="ORIGINAL_EVIDENCE_REPLAYED",
            cost_assessment=costs, cost_record_sha256=digest(cost_record),
            full_net_ev=costs["full_net_ev"], full_net_ev_status=costs["full_net_ev_status"],
        )
        # Rule/book precedence is retained. Missing costs remain explicit even
        # though the generic unintegrated-adapter message is no longer accurate.
        if costs["full_net_ev"] is not None:
            blockers.remove("COST_UNKNOWN")
            record["status"] = blockers[0] if blockers else (
                "POSITIVE_NET_EV_RESEARCH" if Decimal(costs["full_net_ev"]) > 0
                else "NEGATIVE_NET_EV_RESEARCH"
            )
        record["research_blockers"] = list(dict.fromkeys(blockers + costs["blockers"]))
    return record
