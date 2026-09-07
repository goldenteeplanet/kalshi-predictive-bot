from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4ez_workstream_iv_final_gate.py"
SPEC = importlib.util.spec_from_file_location("phase4ez", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def payload():
    return _seal(
        {
            "schema": MODULE.INPUT_SCHEMA,
            "phase_artifacts": [
                {"phase": phase, "artifact_hash": ("abcdef"[index % 6]) * 64}
                for index, phase in enumerate(MODULE.REQUIRED_PHASES)
            ],
            "proofs": [
                {
                    "category": category,
                    "source_phases": sorted(requirement["source_phases"]),
                    "assertion_codes": sorted(requirement["assertion_codes"]),
                    "passed": True,
                    "evidence_hash": ("abcdef"[index % 6]) * 64,
                }
                for index, (category, requirement) in enumerate(MODULE.PROOF_REQUIREMENTS.items())
            ],
            "safety_state": dict.fromkeys(MODULE.SAFETY_FIELDS, False),
        }
    )


def test_complete_workstream_iv_gate_certifies():
    report = MODULE.build_report(payload())
    assert report["phase_artifact_count"] == 25
    assert report["proof_categories"] == sorted(MODULE.PROOF_REQUIREMENTS)
    assert report["all_required_proofs_passed"] is True
    assert report["no_execution_invariants_passed"] is True
    assert report["workstream_iv_certified"] is True
    assert report["certification_state"] == "WORKSTREAM_IV_PAPER_ONLY_ACCELERATION_CERTIFIED"


@pytest.mark.parametrize("category", MODULE.PROOF_REQUIREMENTS)
def test_each_required_proof_category_is_present(category):
    report = MODULE.build_report(payload())
    proof = next(item for item in report["proofs"] if item["category"] == category)
    requirement = MODULE.PROOF_REQUIREMENTS[category]
    assert set(proof["source_phases"]) == requirement["source_phases"]
    assert set(proof["assertion_codes"]) == requirement["assertion_codes"]
    assert proof["passed"] is True


def test_missing_phase_artifact_fails_closed():
    value = payload()
    value["phase_artifacts"].pop()
    _seal(value)
    with pytest.raises(ValueError, match="PHASE_ARTIFACT_SET"):
        MODULE.build_report(value)


def test_duplicate_phase_artifact_fails_closed():
    value = payload()
    value["phase_artifacts"][-1] = value["phase_artifacts"][0]
    _seal(value)
    with pytest.raises(ValueError, match="PHASE_ARTIFACT_ID"):
        MODULE.build_report(value)


def test_unknown_phase_artifact_fails_closed():
    value = payload()
    value["phase_artifacts"][0]["phase"] = "4EZ"
    _seal(value)
    with pytest.raises(ValueError, match="PHASE_ARTIFACT_ID"):
        MODULE.build_report(value)


def test_bad_phase_artifact_hash_fails_closed():
    value = payload()
    value["phase_artifacts"][0]["artifact_hash"] = "bad"
    _seal(value)
    with pytest.raises(ValueError, match="PHASE_ARTIFACT_HASH"):
        MODULE.build_report(value)


def test_missing_proof_fails_closed():
    value = payload()
    value["proofs"].pop()
    _seal(value)
    with pytest.raises(ValueError, match="PROOF_SET"):
        MODULE.build_report(value)


def test_duplicate_proof_category_fails_closed():
    value = payload()
    value["proofs"][-1] = value["proofs"][0]
    _seal(value)
    with pytest.raises(ValueError, match="PROOF_CATEGORY"):
        MODULE.build_report(value)


@pytest.mark.parametrize("category", MODULE.PROOF_REQUIREMENTS)
def test_any_failed_required_proof_fails_closed(category):
    value = payload()
    next(item for item in value["proofs"] if item["category"] == category)["passed"] = False
    _seal(value)
    with pytest.raises(ValueError, match="REQUIRED_PROOF_FAILED"):
        MODULE.build_report(value)


@pytest.mark.parametrize("category", MODULE.PROOF_REQUIREMENTS)
def test_missing_assertion_from_each_proof_fails_closed(category):
    value = payload()
    next(item for item in value["proofs"] if item["category"] == category)["assertion_codes"].pop()
    _seal(value)
    with pytest.raises(ValueError, match="PROOF_ASSERTIONS"):
        MODULE.build_report(value)


def test_extra_assertion_fails_closed():
    value = payload()
    value["proofs"][0]["assertion_codes"].append("UNDECLARED")
    _seal(value)
    with pytest.raises(ValueError, match="PROOF_ASSERTIONS"):
        MODULE.build_report(value)


def test_wrong_source_phase_fails_closed():
    value = payload()
    value["proofs"][0]["source_phases"] = ["4EA"]
    _seal(value)
    with pytest.raises(ValueError, match="SOURCE_PHASES"):
        MODULE.build_report(value)


def test_bad_proof_evidence_hash_fails_closed():
    value = payload()
    value["proofs"][0]["evidence_hash"] = "bad"
    _seal(value)
    with pytest.raises(ValueError, match="EVIDENCE_HASH"):
        MODULE.build_report(value)


@pytest.mark.parametrize("field", MODULE.SAFETY_FIELDS)
def test_any_unsafe_state_fails_no_execution_gate(field):
    value = payload()
    value["safety_state"][field] = True
    _seal(value)
    with pytest.raises(ValueError, match="NO_EXECUTION_INVARIANT"):
        MODULE.build_report(value)


def test_nonboolean_safety_state_fails_closed():
    value = payload()
    value["safety_state"]["services_controlled"] = 0
    _seal(value)
    with pytest.raises(ValueError, match="SAFETY_STATUS"):
        MODULE.build_report(value)


def test_input_order_is_normalized():
    first = MODULE.build_report(payload())
    value = payload()
    value["phase_artifacts"].reverse()
    value["proofs"].reverse()
    for proof in value["proofs"]:
        proof["source_phases"].reverse()
        proof["assertion_codes"].reverse()
    _seal(value)
    second = MODULE.build_report(value)
    assert first["phase_artifacts"] == second["phase_artifacts"]
    assert first["proofs"] == second["proofs"]


def test_input_tampering_fails_closed():
    value = payload()
    value["proofs"][0]["passed"] = False
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "gate.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_report_never_enables_mutates_or_authorizes():
    report = MODULE.build_report(payload())
    for field in MODULE.SAFETY_FIELDS:
        assert report[field] is False
    assert report["paper_orders_created"] == 0


def test_source_has_no_connected_or_mutating_surface():
    source = SCRIPT.read_text()
    for token in (
        "import sqlite3",
        "import requests",
        "import subprocess",
        "systemctl ",
        "exchange_client.",
        "create_order(",
        "insert_order(",
        "/home/james",
    ):
        assert token not in source
