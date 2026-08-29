from __future__ import annotations

import copy

from scripts.local.phase4nv_witness_placement import (
    DOMAINS,
    audit_placement,
    effective_independent_count,
    enumerate_domain_failures,
)


def _witness(index):
    row = {
        "witness_id": f"w{index}",
        "machine": f"machine-{index}",
        "wsl_distribution": f"runtime-{index}",
        "host_os": f"host-{index}",
        "storage_device": f"disk-{index}",
        "network": f"network-{index}",
        "power": f"power-{index}",
        "administrator": f"admin-{index}",
        "software_build": f"build-{index}",
        "signing_key_authority": f"authority-{index}",
        "geography": f"region-{index}",
    }
    row["evidence"] = {domain: f"evidence:{domain}:{index}" for domain in DOMAINS}
    return row


def _diverse():
    return [_witness(index) for index in range(4)]


def _audit(witnesses=None, **kwargs):
    return audit_placement(
        witnesses or _diverse(),
        threshold=3,
        claimed_byzantine_tolerance=1,
        claimed_independent_witnesses=kwargs.get("claimed", 4),
        maximum_failure_cases=kwargs.get("maximum", 256),
    )


def test_diverse_three_of_four_placement_is_evidence_backed() -> None:
    result = _audit()
    assert result["verdict"] == "PASS"
    assert result["effective_independent_witnesses"] == 4
    assert all(count == 4 for count in result["domain_diversity"].values())
    assert result["migration_priorities"] == []


def test_shared_hidden_dependencies_collapse_effective_independence() -> None:
    witnesses = _diverse()
    for witness in witnesses:
        witness["machine"] = "same-machine"
        witness["wsl_distribution"] = "Ubuntu"
        witness["host_os"] = "same-windows-host"
        witness["storage_device"] = "same-disk"
        witness["administrator"] = "same-admin"
        witness["software_build"] = "same-build"
        witness["signing_key_authority"] = "same-authority"
    result = _audit(witnesses)
    assert result["verdict"] == "REFUSE"
    assert result["effective_independent_witnesses"] == 1
    for code in (
        "COLOCATED_QUORUM",
        "SHARED_SIGNING_AUTHORITY",
        "SHARED_MUTABLE_STORAGE",
        "CORRELATED_ADMINISTRATOR_CONTROL",
        "COMMON_BUILD_COMPROMISE",
        "INSUFFICIENT_DOMAIN_DIVERSITY",
        "PLACEMENT_CLAIM_EXCEEDS_EVIDENCE",
    ):
        assert code in result["errors"]
    assert result["migration_priorities"]


def test_missing_evidence_undocumented_dependency_and_excess_claim_refuse() -> None:
    missing = _diverse()
    del missing[0]["evidence"]["power"]
    assert "PLACEMENT_EVIDENCE_MISSING" in _audit(missing)["errors"]
    undocumented = _diverse()
    undocumented[0]["undocumented_dependencies"] = ["shared-hypervisor"]
    assert "UNDOCUMENTED_DEPENDENCIES" in _audit(undocumented)["errors"]
    shared = _diverse()
    shared[1]["network"] = shared[0]["network"]
    assert "PLACEMENT_CLAIM_EXCEEDS_EVIDENCE" in _audit(shared)["errors"]


def test_domain_outage_and_compromise_enumeration_preserves_claim_when_diverse() -> None:
    result = enumerate_domain_failures(_diverse(), threshold=3, maximum_cases=256)
    assert result["verdict"] == "PASS"
    assert len(result["cases"]) == 80
    assert all(row["classification"] == "BOTH" for row in result["cases"])


def test_correlated_domain_failure_exposes_liveness_and_safety_risk() -> None:
    witnesses = _diverse()
    for witness in witnesses[:3]:
        witness["power"] = "shared-ups"
    result = _audit(witnesses)
    assert result["verdict"] == "REFUSE"
    assert any("power:shared-ups" in risk for risk in result["residual_common_mode_risks"])


def test_enumeration_bound_and_duplicate_identity_fail_closed() -> None:
    assert "DOMAIN_ENUMERATION_BOUND_EXCEEDED" in _audit(maximum=5)["errors"]
    duplicate = _diverse()
    duplicate[1]["witness_id"] = duplicate[0]["witness_id"]
    assert "WITNESS_IDENTITY_INVALID" in _audit(duplicate)["errors"]


def test_effective_count_is_deterministic_and_input_preserving() -> None:
    witnesses = _diverse()
    original = copy.deepcopy(witnesses)
    assert effective_independent_count(witnesses) == effective_independent_count(witnesses) == 4
    assert _audit(witnesses) == _audit(witnesses)
    assert witnesses == original


def test_placement_model_has_no_execution_capability() -> None:
    safety = _audit()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
