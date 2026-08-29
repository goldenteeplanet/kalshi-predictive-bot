from __future__ import annotations

import copy

from scripts.local.phase4nx_migration_authorization import reviewer_registry
from scripts.local.phase4ny_cutover_ceremony import inject_operator_fault, simulate_ceremony
from scripts.local.phase4nz_cutover_certification import (
    REQUIRED_FAULTS,
    create_evidence_bundle,
    independently_verify_bundle,
)
from tests.test_phase4nx_migration_authorization import _fixture as authorization_fixture
from tests.test_phase4ny_cutover_ceremony import _fixture as ceremony_fixture


def _bundle():
    plan, request, _, _, approvals = authorization_fixture()
    ceremony_plan, grant, ceremony = ceremony_fixture()
    assert plan == ceremony_plan
    result = simulate_ceremony(
        ceremony,
        plan,
        grant,
        used_authorization_ids=set(),
        maximum_observation_age=8,
        maximum_step_gap=8,
    )
    failures = []
    for fault in sorted(REQUIRED_FAULTS):
        outcome = simulate_ceremony(
            inject_operator_fault(ceremony, fault),
            plan,
            grant,
            used_authorization_ids=set(),
            maximum_observation_age=8,
            maximum_step_gap=8,
        )
        failures.append(
            {
                "fault": fault,
                "verdict": outcome["verdict"],
                "terminal_state": outcome["terminal_state"],
                "transcript_root_sha256": outcome["transcript_root_sha256"],
            }
        )
    return create_evidence_bundle(
        plan=plan,
        authorization_request=request,
        approvals=approvals,
        ceremony=ceremony,
        ceremony_result=result,
        failure_evidence=failures,
    )


def _verify(bundle=None):
    return independently_verify_bundle(bundle or _bundle(), reviewer_registry=reviewer_registry())


def test_complete_bundle_is_deterministic_and_independently_certified() -> None:
    first = _bundle()
    assert first == _bundle()
    result = _verify(first)
    assert result["verdict"] == "PASS"
    assert len(result["reconstruction"]["faults"]) == 13


def test_missing_altered_reordered_and_schema_drift_refuse() -> None:
    missing = _bundle()
    del missing["artifacts"]["plan"]
    assert "EVIDENCE_MISSING" in _verify(missing)["errors"]
    altered = _bundle()
    altered["artifacts"]["checkpoint_anchor"] = "changed"
    assert "EVIDENCE_HASH_MISMATCH" in _verify(altered)["errors"]
    reordered = _bundle()
    reordered["artifacts"]["ceremony"]["events"].reverse()
    assert "EVIDENCE_HASH_MISMATCH" in _verify(reordered)["errors"]
    schema = _bundle()
    schema["schema"] = "future"
    assert "EVIDENCE_SCHEMA_INVALID" in _verify(schema)["errors"]


def test_role_collision_approval_drift_and_unauthorized_stage_refuse() -> None:
    role = _bundle()
    role["artifacts"]["operator_identities"]["verifier"] = role["artifacts"]["operator_identities"][
        "executor"
    ]
    _rehash(role, "operator_identities")
    assert "ROLE_COLLISION" in _verify(role)["errors"]
    approval = _bundle()
    approval["artifacts"]["approvals"][0]["signature_sha256"] = "0" * 64
    _rehash(approval, "approvals")
    assert "APPROVAL_SIGNATURE_DRIFT" in _verify(approval)["errors"]
    scope = _bundle()
    scope["artifacts"]["authorization_request"]["stage_end"] = 999
    _rehash(scope, "authorization_request")
    assert "APPROVAL_REQUEST_DRIFT" in _verify(scope)["errors"]


def test_transcript_chain_terminal_fault_coverage_and_rollback_ambiguity_refuse() -> None:
    chain = _bundle()
    chain["artifacts"]["ceremony_result"]["transcript"][1]["previous_transcript_sha256"] = "0" * 64
    _rehash(chain, "ceremony_result")
    assert "TRANSCRIPT_ROOT_MISMATCH" in _verify(chain)["errors"]
    terminal = _bundle()
    terminal["artifacts"]["ceremony_result"]["terminal_state"] = "PAUSED"
    _rehash(terminal, "ceremony_result")
    assert "CEREMONY_NONTERMINAL" in _verify(terminal)["errors"]
    faults = _bundle()
    faults["artifacts"]["failure_evidence"].pop()
    _rehash(faults, "failure_evidence")
    assert "FAULT_COVERAGE_INCOMPLETE" in _verify(faults)["errors"]
    rollback = _bundle()
    rollback["artifacts"]["rollback_anchor"] = ""
    _rehash(rollback, "rollback_anchor")
    assert "ROLLBACK_AMBIGUITY" in _verify(rollback)["errors"]


def test_unsafe_capability_and_manifest_or_envelope_hash_drift_refuse() -> None:
    unsafe = _bundle()
    unsafe["artifacts"]["safety"]["live_execution"] = True
    _rehash(unsafe, "safety")
    assert "UNSAFE_CAPABILITY_STATE" in _verify(unsafe)["errors"]
    manifest = _bundle()
    manifest["manifest"]["plan"]["sha256"] = "0" * 64
    assert "EVIDENCE_HASH_MISMATCH" in _verify(manifest)["errors"]
    envelope = _bundle()
    envelope["bundle_sha256"] = "0" * 64
    assert "EVIDENCE_BUNDLE_HASH_MISMATCH" in _verify(envelope)["errors"]


def test_independent_reconstruction_is_deterministic_and_input_preserving() -> None:
    bundle = _bundle()
    original = copy.deepcopy(bundle)
    assert _verify(bundle) == _verify(bundle)
    assert bundle == original


def test_certification_has_no_infrastructure_or_trading_capability() -> None:
    safety = _verify()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")


def _rehash(bundle, artifact):
    import hashlib
    import json

    value = bundle["artifacts"][artifact]
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    bundle["manifest"][artifact] = {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "canonical_bytes": len(encoded),
    }
    unsigned = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    bundle["bundle_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
