"""Byzantine witness tolerance and quorum-policy sensitivity proof."""

from __future__ import annotations

import hashlib
import itertools
import json

SCHEMA = "phase4nu.byzantine-policy.v1"
ROLES = (
    "HONEST",
    "OFFLINE",
    "DELAYED",
    "EQUIVOCATING",
    "COLLUDING",
    "FORGED",
    "COMPROMISED",
    "REVOKED",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def analyze_policy(
    witness_count: int,
    threshold: int,
    *,
    claimed_byzantine_tolerance: int,
) -> dict[str, object]:
    errors = []
    if witness_count < 1 or threshold < 1 or threshold > witness_count:
        return _policy_result(["POLICY_SHAPE_INVALID"], witness_count, threshold, 0, 0, -1)
    safety_tolerance = max(-1, 2 * threshold - witness_count - 1)
    liveness_tolerance = witness_count - threshold
    simultaneous = max(-1, min(safety_tolerance, liveness_tolerance))
    if claimed_byzantine_tolerance > simultaneous:
        errors.append("CLAIMED_TOLERANCE_EXCEEDS_GUARANTEE")
    if claimed_byzantine_tolerance < 0:
        errors.append("CLAIMED_TOLERANCE_INVALID")
    result = _policy_result(
        errors,
        witness_count,
        threshold,
        safety_tolerance,
        liveness_tolerance,
        simultaneous,
    )
    result["minimum_witnesses_for_claim"] = 3 * claimed_byzantine_tolerance + 1
    result["canonical_threshold_for_claim"] = 2 * claimed_byzantine_tolerance + 1
    return result


def classify_failure(
    witness_count: int,
    threshold: int,
    *,
    byzantine: int,
    unavailable: int,
) -> dict[str, object]:
    safety = byzantine <= 2 * threshold - witness_count - 1
    liveness = threshold <= witness_count - byzantine - unavailable
    classification = (
        "BOTH"
        if safety and liveness
        else "SAFETY_ONLY"
        if safety
        else "LIVENESS_ONLY"
        if liveness
        else "NEITHER"
    )
    return {
        "byzantine": byzantine,
        "unavailable": unavailable,
        "safety": safety,
        "liveness": liveness,
        "classification": classification,
    }


def enumerate_failures(
    witness_count: int,
    threshold: int,
    *,
    maximum_faults: int,
    maximum_cases: int,
) -> dict[str, object]:
    errors = []
    cases = []
    if maximum_faults < 0 or maximum_cases < 1 or witness_count > 12:
        errors.append("ENUMERATION_BOUND_INVALID")
    else:
        witnesses = tuple(range(witness_count))
        for fault_count in range(maximum_faults + 1):
            for faulty in itertools.combinations(witnesses, fault_count):
                for byzantine_count in range(fault_count + 1):
                    byzantine = tuple(faulty[:byzantine_count])
                    unavailable = tuple(faulty[byzantine_count:])
                    outcome = classify_failure(
                        witness_count,
                        threshold,
                        byzantine=len(byzantine),
                        unavailable=len(unavailable),
                    )
                    body = {
                        "faulty_witnesses": list(faulty),
                        "byzantine_witnesses": list(byzantine),
                        "unavailable_witnesses": list(unavailable),
                        **outcome,
                    }
                    cases.append({**body, "case_sha256": _digest(body)})
                    if len(cases) > maximum_cases:
                        errors.append("ENUMERATION_BOUND_EXCEEDED")
                        cases = cases[:maximum_cases]
                        break
                if errors:
                    break
            if errors:
                break
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "case_count": len(cases),
        "cases": cases,
        "safety": _safety(),
    }
    result["enumeration_sha256"] = _digest(result)
    return result


def scenario_matrix(witness_count: int, threshold: int) -> dict[str, object]:
    specifications = (
        ("HEALTHY", 0, 0),
        ("ONE_OFFLINE", 0, 1),
        ("ONE_DELAYED", 0, 1),
        ("ONE_EQUIVOCATING", 1, 0),
        ("MINORITY_COLLUSION", max(1, threshold - 1), 0),
        ("FORGED_REJECTED", 0, 1),
        ("ONE_COMPROMISED", 1, 0),
        ("COMPROMISED_REVOKED", 0, 1),
        ("CORRELATED_TWO_OFFLINE", 0, 2),
        ("ROTATION_OVERLAP_LOSS", 0, 1),
        ("DELAYED_RESTORATION", 0, 1),
        ("QUORUM_RESTORED", 0, 0),
    )
    rows = []
    for name, byzantine, unavailable in specifications:
        outcome = classify_failure(
            witness_count,
            threshold,
            byzantine=byzantine,
            unavailable=unavailable,
        )
        rows.append({"scenario": name, **outcome})
    result = {
        "schema": SCHEMA,
        "witness_count": witness_count,
        "threshold": threshold,
        "scenario_count": len(rows),
        "scenarios": rows,
        "safety": _safety(),
    }
    result["matrix_sha256"] = _digest(result)
    return result


def compare_policies(policies: list[dict[str, int]]) -> dict[str, object]:
    rows = []
    for policy in policies:
        analysis = analyze_policy(
            policy["witness_count"],
            policy["threshold"],
            claimed_byzantine_tolerance=policy.get("claimed_byzantine_tolerance", 0),
        )
        rows.append(
            {
                "policy": policy,
                "verdict": analysis["verdict"],
                "safety_tolerance": analysis["safety_tolerance"],
                "liveness_tolerance": analysis["liveness_tolerance"],
                "simultaneous_tolerance": analysis["simultaneous_tolerance"],
                "policy_sha256": analysis["policy_sha256"],
            }
        )
    eligible = [row for row in rows if row["verdict"] == "PASS"]
    recommended = max(
        eligible,
        key=lambda row: (
            row["simultaneous_tolerance"],
            row["liveness_tolerance"],
            -row["policy"]["witness_count"],
            -row["policy"]["threshold"],
        ),
        default=None,
    )
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if recommended else "REFUSE",
        "policies": rows,
        "recommended_policy": recommended["policy"] if recommended else None,
        "residual_risk": (
            "Safety halts when Byzantine faults exceed intersection guarantees; "
            "liveness halts when available honest witnesses fall below threshold."
        ),
        "safety": _safety(),
    }
    result["comparison_sha256"] = _digest(result)
    return result


def assess_policy_change(current: dict[str, int], candidate: dict[str, int]) -> dict[str, object]:
    current_result = analyze_policy(
        current["witness_count"],
        current["threshold"],
        claimed_byzantine_tolerance=current.get("claimed_byzantine_tolerance", 0),
    )
    candidate_result = analyze_policy(
        candidate["witness_count"],
        candidate["threshold"],
        claimed_byzantine_tolerance=candidate.get("claimed_byzantine_tolerance", 0),
    )
    errors = []
    if candidate_result["verdict"] != "PASS":
        errors.extend(candidate_result["errors"])
    if candidate_result["simultaneous_tolerance"] < current_result["simultaneous_tolerance"]:
        errors.append("EMERGENCY_POLICY_WEAKENS_TOLERANCE")
    selected = current if errors else candidate
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "selected_policy": selected,
        "rollback_applied": bool(errors),
        "safety": _safety(),
    }
    result["change_sha256"] = _digest(result)
    return result


def _policy_result(
    errors, witness_count, threshold, safety_tolerance, liveness_tolerance, simultaneous
):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "witness_count": witness_count,
        "threshold": threshold,
        "safety_tolerance": safety_tolerance,
        "liveness_tolerance": liveness_tolerance,
        "simultaneous_tolerance": simultaneous,
        "safety": _safety(),
    }
    result["policy_sha256"] = _digest(result)
    return result


def _safety():
    return {
        "offline_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
