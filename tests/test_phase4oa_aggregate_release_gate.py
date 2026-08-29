from __future__ import annotations

import copy
from pathlib import Path

from scripts.local.phase4oa_aggregate_release_gate import (
    BLOCKED_ON_SETTLEMENT,
    CHECK_CATEGORIES,
    PHASES,
    build_release_candidate,
    inventory_phases,
    verify_release_candidate,
)

ROOT = Path(__file__).resolve().parents[1]


def _safety():
    return {
        "offline_only": True,
        "infrastructure_mutation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }


def _checks():
    return {
        category: {
            "verdict": "PASS",
            "deterministic": True,
            "proof_sha256": f"proof-{category}",
            "refusal_classes_tested": [f"{category.upper()}_REFUSAL"],
            "safety": _safety(),
        }
        for category in CHECK_CATEGORIES
    }


def test_inventory_finds_every_phase_script_test_doc_schema_and_rollback() -> None:
    result = inventory_phases(ROOT)
    assert result["verdict"] == "PASS", result["errors"]
    assert result["phase_count"] == len(PHASES) == 27
    assert all(len(row["files"]) == 3 for row in result["phases"])
    assert all(row["schema"] for row in result["phases"])


def test_aggregate_release_candidate_is_deterministic_and_certified() -> None:
    first = build_release_candidate(ROOT, _checks())
    assert first == build_release_candidate(ROOT, _checks())
    assert first["verdict"] == "PASS"
    assert len(first["representative_checks"]) == 13
    assert len(first["dependency_graph"]) == 26
    assert verify_release_candidate(first, ROOT)["verdict"] == "PASS"


def test_missing_phase_and_stale_file_evidence_refuse(monkeypatch) -> None:
    candidate = build_release_candidate(ROOT, _checks())
    candidate["inventory"]["phases"][0]["files"]["script"]["sha256"] = "0" * 64
    assert "AGGREGATE_MANIFEST_HASH_MISMATCH" in verify_release_candidate(candidate, ROOT)["errors"]
    assert "STALE_OR_ALTERED_PHASE_EVIDENCE" in verify_release_candidate(candidate, ROOT)["errors"]


def test_missing_failed_nondeterministic_unsafe_and_unrefused_checks_refuse() -> None:
    missing = _checks()
    missing.pop("golden")
    assert "REPRESENTATIVE_CHECK_MISSING" in build_release_candidate(ROOT, missing)["errors"]
    mutations = (
        ("verdict", "REFUSE", "CONTRADICTORY_OR_FAILED_VERDICT"),
        ("deterministic", False, "NONDETERMINISTIC_OR_UNBOUND_PROOF"),
        ("refusal_classes_tested", [], "UNTESTED_REFUSAL_CLASS"),
    )
    for field, value, expected in mutations:
        checks = _checks()
        checks["golden"][field] = value
        assert any(expected in error for error in build_release_candidate(ROOT, checks)["errors"])
    unsafe = _checks()
    unsafe["golden"]["safety"]["live_execution"] = True
    assert any(
        "EXECUTION_CAPABILITY_EXPOSURE" in error
        for error in build_release_candidate(ROOT, unsafe)["errors"]
    )


def test_schema_dependency_blocker_and_safety_drift_refuse() -> None:
    schema = build_release_candidate(ROOT, _checks())
    schema["schema"] = "future"
    assert "RELEASE_SCHEMA_INVALID" in verify_release_candidate(schema, ROOT)["errors"]
    dependency = build_release_candidate(ROOT, _checks())
    dependency["dependency_graph"].pop()
    _rehash(dependency)
    assert "DEPENDENCY_GRAPH_INCOMPLETE" in verify_release_candidate(dependency, ROOT)["errors"]
    blocker = build_release_candidate(ROOT, _checks())
    blocker["blocked_on_september_1_settlement"] = "none"
    _rehash(blocker)
    assert "SETTLEMENT_BLOCKER_STATEMENT_DRIFT" in verify_release_candidate(blocker, ROOT)["errors"]
    safety = build_release_candidate(ROOT, _checks())
    safety["safety"]["paper_order_creation"] = True
    _rehash(safety)
    assert (
        "AGGREGATE_SAFETY_INVARIANT_VIOLATION" in verify_release_candidate(safety, ROOT)["errors"]
    )


def test_settlement_blocker_is_explicit_and_does_not_block_offline_candidate() -> None:
    candidate = build_release_candidate(ROOT, _checks())
    assert candidate["verdict"] == "PASS"
    assert candidate["blocked_on_september_1_settlement"] == BLOCKED_ON_SETTLEMENT
    assert "KXRAINAUSM-26AUG-1" in BLOCKED_ON_SETTLEMENT
    assert "September 1, 2026" in BLOCKED_ON_SETTLEMENT


def test_gate_is_input_preserving_and_has_no_execution_capability() -> None:
    checks = _checks()
    original = copy.deepcopy(checks)
    candidate = build_release_candidate(ROOT, checks)
    assert checks == original
    safety = candidate["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")


def _rehash(candidate):
    import hashlib
    import json

    unsigned = {key: value for key, value in candidate.items() if key != "manifest_sha256"}
    candidate["manifest_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
