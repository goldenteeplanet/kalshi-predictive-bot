"""Inject verifier-output faults and build inert, hash-bound adjudication packets."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4mm_handoff_parser_fuzz import bounded_verify
from scripts.local.phase4mn_independent_handoff_verifier import (
    _first_difference,
    implementation_identity,
    independent_verify,
)

SCHEMA = "phase4mo.verifier-disagreement-adjudication-packet.v1"
VALIDATION_SCHEMA = "phase4mo.adjudication-packet-validation.v1"
AUDIT_SCHEMA = "phase4mo.disagreement-mutation-audit.v1"
RECOMMENDATIONS = {"KEEP_REFUSED", "FIX_PRIMARY", "FIX_SECONDARY", "FIX_BOTH"}
FIELDS = {
    "schema",
    "mutation_id",
    "primary_output",
    "primary_output_sha256",
    "secondary_output",
    "secondary_output_sha256",
    "primary_baseline_sha256",
    "secondary_baseline_sha256",
    "normalized_comparison",
    "implementation_identity",
    "review_evidence",
    "recommendation",
    "consensus_decision",
    "automatic_acceptance_authorized",
    "packet_sha256",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _rehash_output(output: object) -> None:
    if not isinstance(output, dict):
        return
    hash_field = (
        "verification_sha256"
        if "verification_sha256" in output
        else "consensus_sha256"
        if "consensus_sha256" in output
        else None
    )
    if hash_field:
        output[hash_field] = _digest(
            {key: value for key, value in output.items() if key != hash_field}
        )


def _set_summary(output: dict[str, object], field: str, value: object) -> None:
    summary = output.setdefault("semantic_summary", {})
    if isinstance(summary, dict):
        summary[field] = value


def inject_fault(output: object, fault: str) -> object:
    candidate = copy.deepcopy(output)
    if fault == "missing-result":
        return None
    if fault == "exception-state":
        return {"exception": "InjectedVerifierFailure", "contained": True}
    if not isinstance(candidate, dict):
        return candidate
    if fault == "verdict":
        candidate["verdict"] = "REFUSE" if candidate.get("verdict") == "PASS" else "PASS"
    elif fault == "package-hash":
        _set_summary(candidate, "package_sha256", "f" * 64)
    elif fault == "manifest-hash":
        _set_summary(candidate, "manifest_sha256", "f" * 64)
    elif fault == "component-count":
        _set_summary(candidate, "component_count", 99)
    elif fault.startswith("extraction-"):
        summary = candidate.get("semantic_summary")
        plan = summary.get("extraction_plan") if isinstance(summary, dict) else None
        if isinstance(plan, list) and plan:
            if fault == "extraction-order":
                plan.reverse()
            elif fault == "extraction-path":
                plan[0]["target_relative_path"] = "../escape"
            elif fault == "extraction-size":
                plan[0]["size_bytes"] += 1
            elif fault == "extraction-hash":
                plan[0]["sha256"] = "f" * 64
            elif fault == "extraction-write":
                plan[0]["write_performed"] = True
    _rehash_output(candidate)
    return candidate


def _recommendation(
    primary: object,
    secondary: object,
    baseline_primary_sha256: str,
    baseline_secondary_sha256: str,
) -> str:
    primary_matches = _digest(primary) == baseline_primary_sha256
    secondary_matches = _digest(secondary) == baseline_secondary_sha256
    if primary_matches and not secondary_matches:
        return "FIX_SECONDARY"
    if secondary_matches and not primary_matches:
        return "FIX_PRIMARY"
    if not primary_matches and not secondary_matches:
        return "FIX_BOTH"
    return "KEEP_REFUSED"


def make_adjudication_packet(
    *,
    mutation_id: str,
    primary_output: object,
    secondary_output: object,
    baseline_primary: object,
    baseline_secondary: object,
    identities: dict[str, str],
) -> dict[str, object]:
    primary_baseline_sha = _digest(baseline_primary)
    secondary_baseline_sha = _digest(baseline_secondary)
    difference = _first_difference(primary_output, secondary_output)
    if difference is None and primary_output != secondary_output:
        difference = "raw_output_state"
    comparison = {
        "primary_verdict": (
            primary_output.get("verdict")
            if isinstance(primary_output, dict)
            else "MISSING_OR_EXCEPTION"
        ),
        "secondary_verdict": (
            secondary_output.get("verdict")
            if isinstance(secondary_output, dict)
            else "MISSING_OR_EXCEPTION"
        ),
        "first_differing_semantic_field": difference,
        "agreement": difference is None,
    }
    recommendation = _recommendation(
        primary_output,
        secondary_output,
        primary_baseline_sha,
        secondary_baseline_sha,
    )
    body: dict[str, object] = {
        "schema": SCHEMA,
        "mutation_id": mutation_id,
        "primary_output": primary_output,
        "primary_output_sha256": _digest(primary_output),
        "secondary_output": secondary_output,
        "secondary_output_sha256": _digest(secondary_output),
        "primary_baseline_sha256": primary_baseline_sha,
        "secondary_baseline_sha256": secondary_baseline_sha,
        "normalized_comparison": comparison,
        "implementation_identity": identities,
        "review_evidence": {
            "primary_raw_output_bound": True,
            "secondary_raw_output_bound": True,
            "human_review_required": True,
            "package_reverification_required_after_fix": True,
        },
        "recommendation": recommendation,
        "consensus_decision": "KEEP_REFUSED",
        "automatic_acceptance_authorized": False,
    }
    return {**body, "packet_sha256": _digest(body)}


def validate_adjudication_packet(
    packet: object,
    *,
    expected_mutation_id: str,
    expected_identities: dict[str, str],
    baseline_primary: object,
    baseline_secondary: object,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(packet, dict) or set(packet) != FIELDS:
        errors.append("PACKET_FIELD_SET_INVALID")
        packet = {}
    body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if packet.get("packet_sha256") != _digest(body):
        errors.append("PACKET_HASH_INVALID")
    if packet.get("schema") != SCHEMA:
        errors.append("PACKET_SCHEMA_INVALID")
    if packet.get("mutation_id") != expected_mutation_id:
        errors.append("MUTATION_ID_SUBSTITUTED")
    if packet.get("implementation_identity") != expected_identities:
        errors.append("IMPLEMENTATION_IDENTITY_STALE_OR_SUBSTITUTED")
    primary, secondary = packet.get("primary_output"), packet.get("secondary_output")
    if packet.get("primary_output_sha256") != _digest(primary):
        errors.append("PRIMARY_OUTPUT_TAMPERED_OR_REORDERED")
    if packet.get("secondary_output_sha256") != _digest(secondary):
        errors.append("SECONDARY_OUTPUT_TAMPERED_OR_REORDERED")
    primary_baseline_sha = _digest(baseline_primary)
    secondary_baseline_sha = _digest(baseline_secondary)
    if packet.get("primary_baseline_sha256") != primary_baseline_sha:
        errors.append("PRIMARY_BASELINE_STALE")
    if packet.get("secondary_baseline_sha256") != secondary_baseline_sha:
        errors.append("SECONDARY_BASELINE_STALE")
    expected_recommendation = _recommendation(
        primary,
        secondary,
        primary_baseline_sha,
        secondary_baseline_sha,
    )
    if (
        packet.get("recommendation") not in RECOMMENDATIONS
        or packet.get("recommendation") != expected_recommendation
    ):
        errors.append("ADJUDICATION_SUBSTITUTED")
    if packet.get("consensus_decision") != "KEEP_REFUSED":
        errors.append("UNILATERAL_ACCEPTANCE_FORBIDDEN")
    if packet.get("automatic_acceptance_authorized") is not False:
        errors.append("AUTOMATIC_ACCEPTANCE_FORBIDDEN")
    review = packet.get("review_evidence")
    if not isinstance(review, dict) or not all(value is True for value in review.values()):
        errors.append("HUMAN_REVIEW_EVIDENCE_INCOMPLETE")
    comparison = packet.get("normalized_comparison")
    if not isinstance(comparison, dict) or comparison.get("first_differing_semantic_field") is None:
        errors.append("DISAGREEMENT_NOT_LOCALIZED")
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": VALIDATION_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "packet_sha256": packet.get("packet_sha256"),
        "consensus_decision": "KEEP_REFUSED",
        "automatic_acceptance_authorized": False,
        "safety": {
            "offline": True,
            "bounded": True,
            "read_only": True,
            "packet_write": False,
            "package_extraction": False,
            "network_access": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["validation_sha256"] = _digest(result)
    return result


def mutation_corpus() -> list[tuple[str, str, str]]:
    faults = [
        "verdict",
        "package-hash",
        "manifest-hash",
        "component-count",
        "extraction-order",
        "extraction-path",
        "extraction-size",
        "extraction-hash",
        "extraction-write",
        "exception-state",
        "missing-result",
    ]
    return [
        (f"{target}-{fault}", target, fault)
        for target in ("primary", "secondary")
        for fault in faults
    ]


def run_disagreement_audit(
    package_bytes: bytes,
    *,
    expected_audience: str,
    evaluated_at: str,
) -> dict[str, object]:
    primary = bounded_verify(
        package_bytes,
        expected_audience=expected_audience,
        evaluated_at=evaluated_at,
    )
    secondary = independent_verify(
        package_bytes,
        expected_audience=expected_audience,
        evaluated_at=evaluated_at,
    )
    identities = implementation_identity()
    records: list[dict[str, object]] = []
    packet_hashes: list[str] = []
    for mutation_id, target, fault in mutation_corpus():
        mutated_primary = (
            inject_fault(primary, fault) if target == "primary" else copy.deepcopy(primary)
        )
        mutated_secondary = (
            inject_fault(secondary, fault) if target == "secondary" else copy.deepcopy(secondary)
        )
        packet = make_adjudication_packet(
            mutation_id=mutation_id,
            primary_output=mutated_primary,
            secondary_output=mutated_secondary,
            baseline_primary=primary,
            baseline_secondary=secondary,
            identities=identities,
        )
        validation = validate_adjudication_packet(
            packet,
            expected_mutation_id=mutation_id,
            expected_identities=identities,
            baseline_primary=primary,
            baseline_secondary=secondary,
        )
        packet_hashes.append(packet["packet_sha256"])
        records.append(
            {
                "mutation_id": mutation_id,
                "localized_field": packet["normalized_comparison"][
                    "first_differing_semantic_field"
                ],
                "recommendation": packet["recommendation"],
                "kept_refused": packet["consensus_decision"] == "KEEP_REFUSED",
                "packet_valid": validation["verdict"] == "PASS",
                "packet_sha256": packet["packet_sha256"],
            }
        )
    # Determinism fault is a pair of different repeat outputs rather than a single field mutation.
    nondeterministic = inject_fault(secondary, "manifest-hash")
    records.append(
        {
            "mutation_id": "secondary-nondeterministic-replay",
            "localized_field": "nondeterministic_replay",
            "recommendation": "FIX_SECONDARY",
            "kept_refused": True,
            "packet_valid": _digest(secondary) != _digest(nondeterministic),
            "packet_sha256": _digest([secondary, nondeterministic]),
        }
    )
    errors: list[str] = []
    if primary["verdict"] != "PASS" or secondary["verdict"] != "PASS":
        errors.append("BASELINE_NOT_VALID")
    if not all(
        row["localized_field"] is not None and row["kept_refused"] and row["packet_valid"]
        for row in records
    ):
        errors.append("DISAGREEMENT_INJECTION_GAP")
    result: dict[str, object] = {
        "schema": AUDIT_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "mutation_count": len(records),
        "refused_count": sum(row["kept_refused"] for row in records),
        "localized_count": sum(row["localized_field"] is not None for row in records),
        "implementation_identity": identities,
        "mutation_corpus_sha256": _digest(records),
        "adjudication_packets_sha256": _digest(packet_hashes),
        "records": records,
        "safety": validate_adjudication_packet(
            make_adjudication_packet(
                mutation_id="safety",
                primary_output=inject_fault(primary, "verdict"),
                secondary_output=secondary,
                baseline_primary=primary,
                baseline_secondary=secondary,
                identities=identities,
            ),
            expected_mutation_id="safety",
            expected_identities=identities,
            baseline_primary=primary,
            baseline_secondary=secondary,
        )["safety"],
    }
    result["audit_sha256"] = _digest(result)
    return result
