"""Assemble original-bound CF research into the coordinator's qualification path.

Replayed supported costs feed the existing qualification arithmetic. Missing
costs remain absent, not zero. This assembler never writes a ledger itself.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.crypto.cost_record import (
    build_cost_record,
    cost_decision_from_qualification,
    replay_cost_record,
)
from kalshi_predictor.overnight_paper.boundary import ExecutionMode
from kalshi_predictor.overnight_paper.cf_source import VERIFIER, CFSourceContext
from kalshi_predictor.overnight_paper.coordinator import PreparedCandidate
from kalshi_predictor.overnight_paper.evaluation_dataset import _validate_stored_observation
from kalshi_predictor.overnight_paper.gate_context import QualificationContext
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance
from kalshi_predictor.overnight_paper.qualification import (
    SEMANTIC_VERIFIERS,
    EvidenceReference,
    GateEvidence,
    NetEV,
    compute_net_ev,
    qualify_candidate,
)
from kalshi_predictor.overnight_paper.rule_verifier import RuleDocument
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.paper.models import PaperDecision


def _qualification_ev_from_replayed_costs(
    assessment: dict[str, Any], inputs: dict[str, Any],
) -> NetEV | None:
    """Internal arithmetic adapter; caller must first replay original evidence.

    This helper is not a verifier or a public evidence intake. The assembler
    obtains assessment only from replay_cost_record, never from stored labels.
    """
    if assessment["full_net_ev"] is None:
        return None
    components = [assessment[name] for name in (
        "exchange_fee", "observed_book_stress", "uncertainty",
    )]
    if (
        assessment["full_net_ev_status"] != "FULL_NET_EV_KNOWN"
        or components[0]["status"] != "CERTIFIED"
        or any(c["value"] is None or c["paper_support"] is not True
               or c["status"] not in ("CERTIFIED", "ESTIMATED_WITH_SUPPORT")
               for c in components)
    ):
        return None
    ev = compute_net_ev(
        model_probability=Decimal(inputs["selected_probability"]),
        executable_price=Decimal(str(inputs["executable_price"])),
        estimated_fee=Decimal(components[0]["value"]),
        slippage_allowance=Decimal(components[1]["value"]),
        uncertainty_buffer=Decimal(components[2]["value"]),
    )
    if (ev.net_ev != Decimal(assessment["full_net_ev"])
            or ev.gross_edge != Decimal(assessment["gross_edge"])):
        raise ValueError("CF_ASSEMBLY_REPLAYED_COST_ARITHMETIC_MISMATCH")
    return ev


def assemble_cf_research_candidate(
    *, paper_decision: PaperDecision, provenance_args: dict[str, Any],
    rule_documents: tuple[RuleDocument, ...] = (), repository: Path | None = None,
    cost_record: dict[str, Any] | None = None,
    evaluation_observation: Artifact | None = None,
) -> PreparedCandidate:
    """Consume existing engine/original records; never invent a new forecast.

    Collector gates replay supplied originals through existing semantic validators.
    Missing rule authority and full costs remain blockers, never assumed passes.
    Full provenance is checked before the diagnostic enters the coordinator.
    """
    verified = verify_complete_provenance(**provenance_args)
    if not verified.passed:
        raise ValueError("CF_ASSEMBLY_PROVENANCE_INVALID:" + ",".join(verified.blockers))
    context = provenance_args["context"]
    inputs = provenance_args["decision"]
    if type(context.cf_context) is not CFSourceContext or type(paper_decision) is not PaperDecision:
        raise ValueError("CF_ASSEMBLY_CONCRETE_INPUTS_REQUIRED")
    if (
        paper_decision.ticker != inputs["ticker"]
        or paper_decision.forecast_id != inputs["forecast_id"]
        or paper_decision.model_name != inputs["model_name"]
        or paper_decision.probability != Decimal(str(inputs["forecast_probability"]))
        or paper_decision.side != inputs.get("side")
        or paper_decision.limit_price != Decimal(str(inputs["executable_price"]))
    ):
        raise ValueError("CF_ASSEMBLY_PAPER_DECISION_MISMATCH")
    source = [
        item for item in context.source_artifacts
        if item.decode().get("clock_basis") == inputs.get("source_kind")
    ]
    if len(source) != 1:
        raise ValueError("CF_ASSEMBLY_ONE_SOURCE_REQUIRED")
    at = aware(inputs["decision_at"])
    now = aware(provenance_args["now"])
    identity = canonical_hash(inputs)
    report = dict(
        schema="overnight-paper-gate-v1", gate=4, decision_id=identity,
        ticker=inputs["ticker"], category="Crypto", verifier=VERIFIER, verdict="PASS",
        sources=[source[0].sha256], validated_at=at.isoformat(),
        valid_until=(at + timedelta(seconds=60)).isoformat(),
    )
    reference = EvidenceReference(
        "cf-source-gate", canonical_hash(report),
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode(),
    )
    evidence = GateEvidence(
        4, identity, "Crypto", inputs["ticker"], VERIFIER, reference,
        sources=(EvidenceReference("cf-original", source[0].sha256, source[0].payload),),
        context=context.cf_context,
    )
    if not evidence.verified(inputs, as_of=now):
        raise ValueError("CF_ASSEMBLY_SOURCE_REVALIDATION_FAILED")
    evidence_items = [evidence]
    public_sources = tuple(
        EvidenceReference("public-original", item.sha256, item.payload)
        for item in context.source_artifacts
        if item.decode().get("clock_basis") == "public_rest_receipt"
    )
    gate_context = QualificationContext(
        Path(__file__).resolve().parents[3] if repository is None else repository,
        rule_documents, context, provenance_args["phase3m"], provenance_args["phase3n"],
    )
    for gate in (1, 2, 3, 5, 9, 12):
        public_report = report | dict(
            gate=gate, verifier=SEMANTIC_VERIFIERS[gate],
            sources=[item.sha256 for item in public_sources],
        )
        evidence_items.append(GateEvidence(
            gate, identity, "Crypto", inputs["ticker"], SEMANTIC_VERIFIERS[gate],
            EvidenceReference(
                "cf-public-gate", canonical_hash(public_report),
                json.dumps(public_report, sort_keys=True, separators=(",", ":")).encode(),
            ),
            sources=public_sources,
            context=gate_context if gate in (3, 9, 12) else None,
        ))
    cost_inputs = cost_decision_from_qualification(inputs)
    if cost_record is None:
        cost_record = build_cost_record(
            decision=cost_inputs,
            selected_probability=Decimal(cost_inputs["selected_probability"]),
            executable_price=Decimal(str(inputs["executable_price"])),
            side=inputs["side"].removeprefix("BUY_"), rule_documents=rule_documents,
        )
    cost_assessment = replay_cost_record(cost_record, expected_decision=cost_inputs)
    if evaluation_observation is not None:
        row = evaluation_observation.decode()
        if (
            row.get("kind") != "observation-v1"
            or row.get("decision_id") != identity or row.get("decision") != inputs
            or row.get("cost_record") != cost_record
        ):
            raise ValueError("CF_ASSEMBLY_EVALUATION_BINDING_MISMATCH")
        _validate_stored_observation(row)
    ev = _qualification_ev_from_replayed_costs(cost_assessment, cost_inputs)
    args = dict(
        ticker=inputs["ticker"], category="Crypto", decision_inputs=inputs,
        decision_id=identity, evidence=tuple(evidence_items), ev=ev,
        minimum_net_ev=Decimal("0.05"), phase3m=provenance_args["phase3m"],
        phase3n=provenance_args["phase3n"],
        mode=ExecutionMode.OBSERVATION_ONLY if ev is None else ExecutionMode.LOCAL_PAPER,
    )
    qualified = qualify_candidate(**args)
    payload = dict(
        qualification_inputs=inputs, qualification_status=qualified.status.value,
        qualification_blockers=list(qualified.blockers),
        cf_context=context.cf_context.to_record(decision_at=at),
        full_net_ev=cost_assessment["full_net_ev"], cost_record=cost_record,
        cost_evidence_status="ORIGINAL_EVIDENCE_REPLAYED",
        evidence_role="CF_RESEARCH_NOT_CURRENT_PAPER_ELIGIBILITY",
    )
    return PreparedCandidate(paper_decision, args, payload, evaluation_observation)
