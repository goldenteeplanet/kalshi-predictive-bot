import importlib.util
import json
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

S = Path(__file__).parents[1] / "scripts/local/phase4fk_final_certification.py"
X = importlib.util.spec_from_file_location("m", S)
assert X and X.loader
M = importlib.util.module_from_spec(X)
X.loader.exec_module(M)


def seal(v):
    v.pop("artifact_hash", None)
    v["artifact_hash"] = canonical_hash(v)
    return v


def payload():
    rows = []
    prev = None
    for i, phase in enumerate(M.REQUIRED):
        digest = f"{i + 1:064x}"
        rows.append({"phase": phase, "artifact_hash": digest, "previous_artifact_hash": prev})
        prev = digest
    return seal(
        {
            "schema": M.SCHEMA,
            "phases": rows,
            "proofs": {
                x: True
                for x in (
                    "no_unexplained_mutation",
                    "no_high_threat",
                    "reproducible_build_ci",
                    "rollback_proof",
                    "replay_proof",
                    "airgapped_acceptance",
                    "readiness_passed",
                    "runtime_invariants_preserved",
                )
            },
            "integrations": ["RECOMMENDATION_ONLY"],
            "residual_risks": [
                {
                    "id": "floating-actions",
                    "severity": "MEDIUM",
                    "status": "DEFERRED",
                    "mitigation": "Pin legacy workflows before making checks required.",
                }
            ],
            "focused_tests_passed": 200,
            "cumulative_tests_passed": 2400,
            "expected_skips": 2,
        }
    )


def test_success_has_only_terminal_state():
    r = M.build_report(payload())
    assert (
        r["phase_count"] == 100
        and r["terminal_state"] == M.TERMINAL
        and r["offline_and_paper_only_acceleration_certified"]
    )


@pytest.mark.parametrize("index", [0, 1, 25, 50, 75, 99])
def test_missing_phase_fails(index):
    v = payload()
    v["phases"].pop(index)
    seal(v)
    with pytest.raises(ValueError, match="PHASE_SET"):
        M.build_report(v)


def test_reordered_phase_fails():
    v = payload()
    v["phases"][1], v["phases"][2] = v["phases"][2], v["phases"][1]
    seal(v)
    with pytest.raises(ValueError, match="PHASE_SET"):
        M.build_report(v)


def test_missing_link_fails():
    v = payload()
    v["phases"][50]["previous_artifact_hash"] = "f" * 64
    seal(v)
    with pytest.raises(ValueError, match="LINEAGE"):
        M.build_report(v)


@pytest.mark.parametrize(
    "proof",
    [
        "no_unexplained_mutation",
        "no_high_threat",
        "reproducible_build_ci",
        "rollback_proof",
        "replay_proof",
        "airgapped_acceptance",
        "readiness_passed",
        "runtime_invariants_preserved",
    ],
)
def test_each_proof_is_mandatory(proof):
    v = payload()
    v["proofs"][proof] = False
    seal(v)
    with pytest.raises(ValueError, match="PROOF"):
        M.build_report(v)


def test_unresolved_high_risk_fails():
    v = payload()
    v["residual_risks"].append(
        {"id": "x", "severity": "HIGH", "status": "DEFERRED", "mitigation": "none"}
    )
    seal(v)
    with pytest.raises(ValueError, match="UNRESOLVED_HIGH"):
        M.build_report(v)


@pytest.mark.parametrize("disposition", ["INSTALLED_WITHOUT_APPROVAL", "UNKNOWN", ""])
def test_unsafe_integration_disposition_fails(disposition):
    v = payload()
    v["integrations"] = [disposition]
    seal(v)
    with pytest.raises(ValueError, match="INTEGRATION"):
        M.build_report(v)


@pytest.mark.parametrize("field", ["focused_tests_passed", "cumulative_tests_passed"])
def test_zero_tests_fail(field):
    v = payload()
    v[field] = 0
    seal(v)
    with pytest.raises(ValueError, match="TESTS_NOT_PASSING"):
        M.build_report(v)


def test_tamper():
    v = payload()
    v["expected_skips"] = 0
    with pytest.raises(ValueError, match="HASH"):
        M.build_report(v)


@pytest.mark.parametrize(
    "field",
    [
        "live_trading_authorized",
        "paper_order_creation_enabled",
        "exchange_enabled",
        "production_database_mutated",
        "services_controlled",
    ],
)
def test_certification_never_authorizes_execution(field):
    assert M.build_report(payload())[field] is False


def test_atomic(tmp_path):
    p = tmp_path / "x"
    r = M.build_report(payload())
    M.publish(p, r)
    assert json.loads(p.read_text()) == r
