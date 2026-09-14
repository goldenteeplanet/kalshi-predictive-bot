"""Replay incomplete preparation records without granting provenance or eligibility."""

from __future__ import annotations

from typing import Any

from kalshi_predictor.crypto.cost_record import (
    cost_decision_from_qualification,
    replay_cost_record,
)
from kalshi_predictor.overnight_paper.cf_source import CLOCK_BASIS
from kalshi_predictor.overnight_paper.store import digest

KIND = "CF_PREPARATION_COST_BLOCK_V1"
PREFIX = "cf-preparation-cost-block-v1:"


def preparation_cost_block_record(
    scope: dict[str, Any], cost_record: dict[str, Any],
) -> dict[str, Any]:
    """Build the same diagnostic at write and read time from original costs.

    Scope references are supplied by the writer from persisted forecast rows.
    Replaying this record is not independent verification of full provenance.
    """
    if (
        scope.get("source_kind") != CLOCK_BASIS
        or digest(cost_decision_from_qualification(scope)) != digest(scope)
    ):
        raise ValueError("CF_PREPARATION_SCOPE_DERIVATION_MISMATCH")
    account = scope.get("account_identity_sha256")
    if (
        not isinstance(account, str) or len(account) != 64
        or any(c not in "0123456789abcdef" for c in account)
        or cost_record.get("request", {}).get("account_identity_sha256") != account
    ):
        raise ValueError("CF_PREPARATION_ACCOUNT_IDENTITY_MISMATCH")
    costs = replay_cost_record(cost_record, expected_decision=scope)
    if costs["full_net_ev"] is not None or costs["full_net_ev_status"] != "FULL_NET_EV_UNKNOWN":
        raise ValueError("CF_PREPARATION_UNKNOWN_COST_BLOCK_REQUIRED")
    return dict(
        kind=KIND, preparation_id=digest(scope), status="PREPARATION_BLOCKED_COST_UNKNOWN",
        decision_inputs=scope, cost_record=cost_record, cost_assessment=costs,
        blockers=list(dict.fromkeys([*costs["blockers"], "FULL_PROVENANCE_NOT_VERIFIED",
                                     "RISK_PREPARATION_INCOMPLETE"])),
        source_binding_status="PERSISTED_FORECAST_REFERENCES_ONLY",
        phase3m_status="NOT_ATTESTED", phase3n_status="NOT_ATTESTED",
        evidence_role="INCOMPLETE_PREPARATION_NOT_SHADOW_OR_PAPER_ELIGIBILITY",
        execution_authority=False,
    )
