"""Witness diversity and common-mode failure proof."""

from __future__ import annotations

import hashlib
import itertools
import json

from scripts.local.phase4nu_byzantine_policy import analyze_policy, classify_failure

SCHEMA = "phase4nv.witness-placement.v1"
DOMAINS = (
    "machine",
    "wsl_distribution",
    "host_os",
    "storage_device",
    "network",
    "power",
    "administrator",
    "software_build",
    "signing_key_authority",
    "geography",
)
STRICT_UNIQUE = {
    "storage_device",
    "administrator",
    "software_build",
    "signing_key_authority",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _value_set(value: object) -> set[str]:
    if isinstance(value, str) and value:
        return {value}
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        return set(value)
    return set()


def _pair_independent(left: dict[str, object], right: dict[str, object]) -> bool:
    return all(
        not (_value_set(left.get(domain)) & _value_set(right.get(domain))) for domain in DOMAINS
    )


def effective_independent_count(witnesses: list[dict[str, object]]) -> int:
    maximum = 0
    for size in range(1, len(witnesses) + 1):
        if any(
            all(_pair_independent(left, right) for left, right in itertools.combinations(group, 2))
            for group in itertools.combinations(witnesses, size)
        ):
            maximum = size
    return maximum


def enumerate_domain_failures(
    witnesses: list[dict[str, object]],
    *,
    threshold: int,
    maximum_cases: int,
) -> dict[str, object]:
    cases = []
    for domain in DOMAINS:
        values = sorted(
            {value for witness in witnesses for value in _value_set(witness.get(domain))}
        )
        for value in values:
            affected = sorted(
                witness["witness_id"]
                for witness in witnesses
                if value in _value_set(witness.get(domain))
            )
            for mode in ("OUTAGE", "COMPROMISE"):
                outcome = classify_failure(
                    len(witnesses),
                    threshold,
                    byzantine=len(affected) if mode == "COMPROMISE" else 0,
                    unavailable=len(affected) if mode == "OUTAGE" else 0,
                )
                body = {
                    "domain": domain,
                    "value": value,
                    "mode": mode,
                    "affected_witnesses": affected,
                    **outcome,
                }
                cases.append({**body, "case_sha256": _digest(body)})
                if len(cases) > maximum_cases:
                    result = {
                        "verdict": "REFUSE",
                        "errors": ["DOMAIN_ENUMERATION_BOUND_EXCEEDED"],
                        "cases": cases[:maximum_cases],
                    }
                    result["enumeration_sha256"] = _digest(result)
                    return result
    result = {"verdict": "PASS", "errors": [], "cases": cases}
    result["enumeration_sha256"] = _digest(result)
    return result


def audit_placement(
    witnesses: object,
    *,
    threshold: int,
    claimed_byzantine_tolerance: int,
    claimed_independent_witnesses: int,
    maximum_failure_cases: int = 256,
) -> dict[str, object]:
    errors = []
    if not isinstance(witnesses, list) or not witnesses:
        return _result(["PLACEMENT_SHAPE_INVALID"], [], {}, 0, None, [], [])
    ids = [row.get("witness_id") for row in witnesses if isinstance(row, dict)]
    if len(ids) != len(witnesses) or len(ids) != len(set(ids)):
        errors.append("WITNESS_IDENTITY_INVALID")
    missing_evidence = []
    undocumented = []
    for witness in witnesses:
        evidence = witness.get("evidence", {}) if isinstance(witness, dict) else {}
        for domain in DOMAINS:
            if not _value_set(witness.get(domain)) or not evidence.get(domain):
                missing_evidence.append(f"{witness.get('witness_id')}:{domain}")
        if witness.get("undocumented_dependencies"):
            undocumented.append(witness.get("witness_id"))
    if missing_evidence:
        errors.append("PLACEMENT_EVIDENCE_MISSING")
    if undocumented:
        errors.append("UNDOCUMENTED_DEPENDENCIES")
    groups = {}
    for domain in DOMAINS:
        for witness in witnesses:
            for value in _value_set(witness.get(domain)):
                groups.setdefault((domain, value), []).append(witness["witness_id"])
    shared = [
        {"domain": domain, "value": value, "witnesses": sorted(members), "count": len(members)}
        for (domain, value), members in sorted(groups.items())
        if len(members) > 1
    ]
    if any(row["count"] >= threshold for row in shared):
        errors.append("COLOCATED_QUORUM")
    special_errors = {
        "signing_key_authority": "SHARED_SIGNING_AUTHORITY",
        "storage_device": "SHARED_MUTABLE_STORAGE",
        "administrator": "CORRELATED_ADMINISTRATOR_CONTROL",
        "software_build": "COMMON_BUILD_COMPROMISE",
    }
    for domain, code in special_errors.items():
        if any(row["domain"] == domain for row in shared):
            errors.append(code)
    diversity = {
        domain: len({value for witness in witnesses for value in _value_set(witness.get(domain))})
        for domain in DOMAINS
    }
    if any(count < threshold for count in diversity.values()):
        errors.append("INSUFFICIENT_DOMAIN_DIVERSITY")
    effective = effective_independent_count(witnesses)
    if claimed_independent_witnesses > effective:
        errors.append("PLACEMENT_CLAIM_EXCEEDS_EVIDENCE")
    policy = analyze_policy(
        len(witnesses),
        threshold,
        claimed_byzantine_tolerance=claimed_byzantine_tolerance,
    )
    if policy["verdict"] != "PASS":
        errors.extend(policy["errors"])
    enumeration = enumerate_domain_failures(
        witnesses, threshold=threshold, maximum_cases=maximum_failure_cases
    )
    if enumeration["verdict"] != "PASS":
        errors.extend(enumeration["errors"])
    priorities = sorted(
        (
            {
                "priority": 1 if row["domain"] in STRICT_UNIQUE else 2,
                "domain": row["domain"],
                "shared_value": row["value"],
                "move_witnesses": row["witnesses"][1:],
            }
            for row in shared
        ),
        key=lambda row: (row["priority"], row["domain"], row["shared_value"]),
    )
    residual = [
        f"{row['mode']}:{row['domain']}:{row['value']}"
        for row in enumeration.get("cases", [])
        if row["classification"] != "BOTH"
    ]
    return _result(
        sorted(set(errors)), witnesses, diversity, effective, enumeration, priorities, residual
    )


def _result(errors, witnesses, diversity, effective, enumeration, priorities, residual):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "witness_count": len(witnesses),
        "effective_independent_witnesses": effective,
        "domain_diversity": diversity,
        "failure_case_count": len(enumeration.get("cases", [])) if enumeration else 0,
        "failure_enumeration_sha256": enumeration.get("enumeration_sha256")
        if enumeration
        else None,
        "migration_priorities": priorities,
        "residual_common_mode_risks": residual,
        "safety": _safety(),
    }
    result["placement_sha256"] = _digest(result)
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
