from __future__ import annotations

import hashlib
import json

import pytest

from scripts.local.phase4lx_attestation_composition_gate import (
    compose_attestations,
    make_attestation,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _artifact(schema, hash_field, **fields):
    result = {"schema": schema, **fields}
    result[hash_field] = _digest(result)
    return result


def _artifacts():
    component = "a" * 64
    request = "b" * 64
    dependency = _artifact(
        "phase4lw.verifier-dependency-readiness.v1",
        "readiness_sha256",
        fixture_readiness="PASS",
        production_readiness="REFUSE",
        components=[{"component_sha256": component}],
    )
    transparency = _artifact(
        "phase4lu.keyless-proof-offline-validation.v1",
        "validation_sha256",
        verdict="PASS",
        request_sha256=request,
        proof_sha256="c" * 64,
        merkle_inclusion_structurally_verified=True,
        cryptographic_signature_verified=False,
    )
    verifier = _artifact(
        "phase4lv.offline-cryptographic-verification.v1",
        "verification_sha256",
        verdict="PASS",
        request_sha256=request,
        fixture_signature_verified=True,
        production_readiness="REFUSE",
        verification_identity_sha256="d" * 64,
    )
    return dependency, transparency, verifier


def _attestations(dependency, transparency, verifier, authenticity="FIXTURE"):
    component = dependency["components"][0]["component_sha256"]
    common = {
        "subject_component_sha256": component,
        "dependency_readiness_sha256": dependency["readiness_sha256"],
        "transparency_validation_sha256": transparency["validation_sha256"],
        "verifier_result_sha256": verifier["verification_sha256"],
        "issued_at": "2026-08-28T19:00:00Z",
        "expires_at": "2026-08-29T19:00:00Z",
        "authenticity_class": authenticity,
        "self_asserted": False,
    }
    return [
        make_attestation(
            **common,
            role="BUILDER",
            authority_id="dejoia-fixture-builder",
            authority_sha256="1" * 64,
            replay_identity_sha256="e" * 64,
        ),
        make_attestation(
            **common,
            role="SCANNER",
            authority_id="dejoia-fixture-scanner",
            authority_sha256="2" * 64,
            replay_identity_sha256="f" * 64,
        ),
    ]


def _inputs(authenticity="FIXTURE"):
    artifacts = _artifacts()
    return (*artifacts, _attestations(*artifacts, authenticity))


def _compose(values=None, at="2026-08-28T20:00:00Z"):
    return compose_attestations(*(values or _inputs()), evaluated_at=at)


def _rehash(attestation):
    attestation["attestation_sha256"] = _digest(
        {key: value for key, value in attestation.items() if key != "attestation_sha256"}
    )


def test_fixture_composition_is_deterministic_pass_but_production_refuses() -> None:
    first = _compose()
    assert first == _compose()
    assert first["fixture_readiness"] == "PASS"
    assert first["production_readiness"] == "REFUSE"


def test_reordered_attestations_refuse() -> None:
    values = list(_inputs())
    values[-1] = list(reversed(values[-1]))
    assert "ATTESTATION_ORDER_INVALID" in _compose(values)["errors"]


@pytest.mark.parametrize(
    "field",
    [
        "subject_component_sha256",
        "dependency_readiness_sha256",
        "transparency_validation_sha256",
        "verifier_result_sha256",
    ],
)
def test_every_cross_artifact_binding_mismatch_refuses(field: str) -> None:
    values = list(_inputs())
    values[-1][0][field] = "0" * 64
    _rehash(values[-1][0])
    assert any(f"BINDING_MISMATCH:{field}" in error for error in _compose(values)["errors"])


def test_distinct_pinned_builder_and_scanner_are_required() -> None:
    values = list(_inputs())
    values[-1][1].update(
        role="BUILDER",
        authority_id="dejoia-fixture-builder",
        authority_sha256="1" * 64,
    )
    _rehash(values[-1][1])
    errors = _compose(values)["errors"]
    assert any("DUPLICATE_ROLE" in error for error in errors)
    assert any("AUTHORITY_NOT_INDEPENDENT" in error for error in errors)


def test_self_asserted_duplicate_replay_and_domain_overlap_refuse() -> None:
    values = list(_inputs())
    values[-1][0]["self_asserted"] = True
    values[-1][1]["replay_identity_sha256"] = values[-1][0]["replay_identity_sha256"]
    _rehash(values[-1][0])
    _rehash(values[-1][1])
    errors = _compose(values)["errors"]
    assert any("SELF_ASSERTED_EVIDENCE" in error for error in errors)
    assert any("DUPLICATE_REPLAY_ID" in error for error in errors)
    values = list(_inputs())
    values[-1][0]["replay_identity_sha256"] = values[1]["proof_sha256"]
    _rehash(values[-1][0])
    assert any("REPLAY_DOMAIN_OVERLAP" in error for error in _compose(values)["errors"])


def test_stale_future_overlong_and_malformed_times_refuse() -> None:
    for issued, expires in (
        ("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"),
        ("2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"),
        ("2026-08-28T00:00:00Z", "2026-08-30T00:00:01Z"),
        ("bad", "also-bad"),
    ):
        values = list(_inputs())
        values[-1][0].update(issued_at=issued, expires_at=expires)
        _rehash(values[-1][0])
        assert _compose(values)["verdict"] == "REFUSE"


def test_partial_or_mismatched_artifact_results_refuse() -> None:
    values = list(_inputs())
    values[1]["merkle_inclusion_structurally_verified"] = False
    values[1]["validation_sha256"] = _digest(
        {key: value for key, value in values[1].items() if key != "validation_sha256"}
    )
    assert "MERKLE_EVIDENCE_INCOMPLETE" in _compose(values)["errors"]
    values = list(_inputs())
    values[2]["request_sha256"] = "0" * 64
    values[2]["verification_sha256"] = _digest(
        {key: value for key, value in values[2].items() if key != "verification_sha256"}
    )
    assert "REQUEST_BINDING_MISMATCH" in _compose(values)["errors"]


def test_self_declared_production_class_cannot_override_upstream_refusals() -> None:
    result = _compose(_inputs("INDEPENDENT_PRODUCTION"))
    assert result["fixture_readiness"] == "PASS"
    assert result["production_readiness"] == "REFUSE"
    assert "DEPENDENCY_PRODUCTION_READINESS_NOT_PASSING" in result["production_reasons"]
    assert "PRODUCTION_AUTHORITY_PINS_UNAVAILABLE" in result["production_reasons"]


def test_artifact_and_attestation_hash_tampering_refuse() -> None:
    values = list(_inputs())
    values[0]["fixture_readiness"] = "REFUSE"
    assert "DEPENDENCY_HASH_MISMATCH" in _compose(values)["errors"]
    values = list(_inputs())
    values[-1][0]["authority_id"] = "other"
    assert any("HASH_MISMATCH" in error for error in _compose(values)["errors"])


def test_gate_has_no_external_or_action_capability() -> None:
    safety = _compose()["safety"]
    assert safety["validation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "validation_only")
