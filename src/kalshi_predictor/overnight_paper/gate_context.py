"""Original verifier inputs supplied by the guarded coordinator, never PASS flags."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.advanced_risk.engine import AdvancedRiskDecision
from kalshi_predictor.overnight_paper.provenance_gate import (
    ProvenanceContext,
    verify_complete_provenance,
)
from kalshi_predictor.overnight_paper.rule_verifier import RuleDocument
from kalshi_predictor.position_sizing.sizer import PositionSizingDecision


@dataclass(frozen=True)
class QualificationContext:
    repository: Path
    rule_documents: tuple[RuleDocument, ...]
    provenance: ProvenanceContext | None = None
    phase3m: PositionSizingDecision | None = None
    phase3n: AdvancedRiskDecision | None = None


def verify_context_gate(
    gate: int,
    *,
    inputs: dict[str, Any],
    context: QualificationContext | None,
    now: datetime,
) -> bool:
    if not isinstance(context, QualificationContext):
        return False
    from kalshi_predictor.overnight_paper import rule_verifier
    from kalshi_predictor.overnight_paper.timing import verify_settlement_horizon

    # Policies are a reviewed code dependency, not accepted from candidate JSON.
    rule = rule_verifier.verify_settlement_rule(
        decision=inputs,
        documents=context.rule_documents,
        registry=rule_verifier.CERTIFIED_RULE_POLICIES,
    )
    if gate == 3:
        return rule.passed and verify_settlement_horizon(decision=inputs, rule=rule, now=now).passed
    if gate == 9:
        if not rule.passed or context.provenance is None:
            return False
        if context.provenance.expected_rule_version != rule.rule_version:
            return False
        from kalshi_predictor.overnight_paper.provenance import canonical_hash

        return verify_complete_provenance(
            decision=inputs,
            decision_id=canonical_hash(inputs),
            context=context.provenance,
            now=now,
            phase3m=context.phase3m,
            phase3n=context.phase3n,
        ).passed
    if gate == 12:
        from kalshi_predictor.overnight_paper.boundary_gate import verify_coordinator_boundary

        return verify_coordinator_boundary(
            repository=context.repository,
            code_sha=inputs.get("code_sha", ""),
            settings=inputs.get("settings", {}),
        ).passed
    return False
