from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from scripts.local.phase4os_program_certification import (
    CATEGORIES,
    PHASES,
    certify_program,
    independently_verify_certificate,
    inventory_program,
)

ROOT = Path(__file__).resolve().parents[1]


def _evidence():
    body = {
        "verdict": "PASS",
        "passed_tests": 100,
        "categories": list(CATEGORIES),
        "command": "pytest tests/test_phase4o[a-r]*.py",
    }
    body["proof_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def test_inventory_has_exact_complete_phase_triplets() -> None:
    inventory = inventory_program(ROOT)
    assert inventory["verdict"] == "PASS"
    assert inventory["phase_count"] == len(PHASES) == 18
    assert inventory["phase_order"] == list(PHASES)
    assert all(
        set(row["files"]) == {"script", "test", "documentation"} for row in inventory["phases"]
    )


def test_certificate_and_independent_verification_pass() -> None:
    evidence = _evidence()
    certificate = certify_program(ROOT, evidence)
    assert certificate["verdict"] == "PASS"
    result = independently_verify_certificate(
        certificate,
        trusted_inventory_sha256=certificate["inventory"]["inventory_sha256"],
        trusted_regression_sha256=evidence["proof_sha256"],
    )
    assert result["verdict"] == "PASS"


def test_missing_category_failed_regression_and_low_test_count_refuse() -> None:
    variants = []
    missing = _evidence()
    missing["categories"] = missing["categories"][:-1]
    variants.append(missing)
    failed = _evidence()
    failed["verdict"] = "REFUSE"
    variants.append(failed)
    low = _evidence()
    low["passed_tests"] = 1
    variants.append(low)
    assert all(certify_program(ROOT, evidence)["verdict"] == "REFUSE" for evidence in variants)


def test_tampering_external_anchor_dependency_safety_and_blocker_refuse() -> None:
    evidence = _evidence()
    certificate = certify_program(ROOT, evidence)
    variants = []
    dependency = copy.deepcopy(certificate)
    dependency["dependency_graph"].pop()
    variants.append((dependency, "DEPENDENCY_GRAPH_INVALID"))
    safety = copy.deepcopy(certificate)
    safety["safety"]["paper_order_creation"] = True
    variants.append((safety, "SAFETY_INVARIANT_VIOLATION"))
    blocker = copy.deepcopy(certificate)
    blocker["blocked_on_september_1_settlement"] = "removed"
    variants.append((blocker, "SETTLEMENT_BLOCKER_DRIFT"))
    for candidate, expected in variants:
        result = independently_verify_certificate(
            candidate,
            trusted_inventory_sha256=certificate["inventory"]["inventory_sha256"],
            trusted_regression_sha256=evidence["proof_sha256"],
        )
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]
    wrong = independently_verify_certificate(
        certificate,
        trusted_inventory_sha256="f" * 64,
        trusted_regression_sha256=evidence["proof_sha256"],
    )
    assert "TRUSTED_INVENTORY_MISMATCH" in wrong["errors"]


def test_certification_is_deterministic_input_preserving_and_execution_free() -> None:
    evidence = _evidence()
    original = copy.deepcopy(evidence)
    first = certify_program(ROOT, evidence)
    second = certify_program(ROOT, evidence)
    assert first == second
    assert evidence == original
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
