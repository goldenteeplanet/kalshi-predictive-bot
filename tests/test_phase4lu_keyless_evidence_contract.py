from __future__ import annotations

import hashlib
import json

import pytest

from scripts.local.phase4lu_keyless_evidence_contract import create_request, validate_proof


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _request():
    return create_request(
        evidence_schema="phase4lt.canonicalization-invariance-audit.v1",
        evidence_sha256="a" * 64,
        issuer="dejoia-local-attestor",
        created_at="2026-08-28T20:00:00Z",
        expires_at="2026-08-28T20:10:00Z",
        raw_nonce="0123456789abcdef",
    )["request"]


def _proof(request):
    certificate = {
        "subject": "dejoia-alert-evidence-builder",
        "issuer": request["issuer"],
        "audience": request["audience"],
        "not_before": "2026-08-28T19:55:00Z",
        "not_after": "2026-08-28T20:15:00Z",
        "public_key_algorithm": "ECDSA-P256-SHA256",
    }
    certificate["certificate_sha256"] = _digest(certificate)
    signature = {
        "algorithm": request["algorithm"],
        "payload_sha256": request["request_sha256"],
        "signature_sha256": "c" * 64,
    }
    leaf = _digest(
        {
            "request_sha256": request["request_sha256"],
            "certificate_sha256": certificate["certificate_sha256"],
            "signature_sha256": signature["signature_sha256"],
        }
    )
    return {
        "schema": "phase4lu.keyless-proof.v1",
        "request_sha256": request["request_sha256"],
        "certificate": certificate,
        "signature": signature,
        "transparency": {
            "log_id": "dejoia-offline-log-v1",
            "leaf_sha256": leaf,
            "root_sha256": leaf,
            "tree_size": 1,
            "leaf_index": 0,
            "inclusion_path": [],
        },
    }


def _validate(request=None, proof=None, replay=None, at="2026-08-28T20:05:00Z"):
    request = request or _request()
    return validate_proof(request, proof or _proof(request), replay or [], evaluated_at=at)


def test_request_is_deterministic_inert_and_omits_raw_nonce() -> None:
    first = _request()
    assert first == _request()
    assert "raw_nonce" not in first
    assert first["safety"]["signing_capability"] is False
    assert first["nonce_sha256"] == hashlib.sha256(b"0123456789abcdef").hexdigest()


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"issuer": "https://issuer.example"}, "ISSUER_NOT_ALLOWLISTED"),
        ({"evidence_schema": "unknown"}, "EVIDENCE_SCHEMA_NOT_ALLOWLISTED"),
        ({"evidence_sha256": "short"}, "EVIDENCE_HASH_INVALID"),
        ({"expires_at": "2026-08-28T21:00:00Z"}, "REQUEST_VALIDITY_INVALID"),
    ],
)
def test_request_authority_and_bounds_fail_closed(kwargs, error: str) -> None:
    values = {
        "evidence_schema": "phase4lt.canonicalization-invariance-audit.v1",
        "evidence_sha256": "a" * 64,
        "issuer": "dejoia-local-attestor",
        "created_at": "2026-08-28T20:00:00Z",
        "expires_at": "2026-08-28T20:10:00Z",
        "raw_nonce": "0123456789abcdef",
    }
    values.update(kwargs)
    assert error in create_request(**values)["errors"]


def test_valid_structural_proof_passes_without_claiming_signature_verification() -> None:
    result = _validate()
    assert result["verdict"] == "PASS"
    assert result["merkle_inclusion_structurally_verified"] is True
    assert result["cryptographic_signature_verified"] is False


@pytest.mark.parametrize(
    "mutation,error",
    [
        (
            lambda proof: proof["certificate"].update(subject="trader"),
            "CERTIFICATE_SUBJECT_MISMATCH",
        ),
        (lambda proof: proof["certificate"].update(issuer="other"), "CERTIFICATE_ISSUER_MISMATCH"),
        (
            lambda proof: proof["certificate"].update(audience="other"),
            "CERTIFICATE_AUDIENCE_MISMATCH",
        ),
        (lambda proof: proof["signature"].update(algorithm="RSA"), "SIGNATURE_ALGORITHM_INVALID"),
        (
            lambda proof: proof["signature"].update(payload_sha256="d" * 64),
            "SIGNATURE_PAYLOAD_MISMATCH",
        ),
        (
            lambda proof: proof["transparency"].update(root_sha256="e" * 64),
            "INCLUSION_ROOT_MISMATCH",
        ),
    ],
)
def test_identity_algorithm_payload_and_inclusion_mutations_refuse(mutation, error: str) -> None:
    request = _request()
    proof = _proof(request)
    mutation(proof)
    assert error in _validate(request, proof)["errors"]


def test_replay_by_proof_or_nonce_refuses() -> None:
    request = _request()
    proof = _proof(request)
    assert "REPLAY_DETECTED" in _validate(request, proof, [_digest(proof)])["errors"]
    assert "REPLAY_DETECTED" in _validate(request, proof, [request["nonce_sha256"]])["errors"]


def test_expired_ambiguous_and_overlong_certificate_time_refuse() -> None:
    assert "REQUEST_NOT_CURRENT" in _validate(at="2026-08-28T20:10:00Z")["errors"]
    request = _request()
    proof = _proof(request)
    proof["certificate"]["not_after"] = "2026-08-29T20:15:00Z"
    assert "CERTIFICATE_VALIDITY_INVALID" in _validate(request, proof)["errors"]
    assert "CLOCK_EVIDENCE_INVALID" in _validate(at="2026-08-28 20:05:00")["errors"]


def test_tampered_request_and_malformed_chain_refuse() -> None:
    request = _request()
    request["audience"] = "broader"
    assert "REQUEST_HASH_MISMATCH" in _validate(request)["errors"]
    request = _request()
    proof = _proof(request)
    proof["transparency"]["inclusion_path"] = [{"side": "up", "sha256": "a" * 64}]
    assert any("INCLUSION_PATH_0" in error for error in _validate(request, proof)["errors"])


def test_rehashed_request_cannot_broaden_safety_or_transparency_authority() -> None:
    request = _request()
    request["safety"]["network_access"] = True
    request["request_sha256"] = _digest(
        {key: value for key, value in request.items() if key != "request_sha256"}
    )
    assert "REQUEST_SAFETY_INVALID" in _validate(request)["errors"]
    request = _request()
    proof = _proof(request)
    proof["transparency"]["log_id"] = "remote-log"
    assert "TRANSPARENCY_LOG_NOT_ALLOWLISTED" in _validate(request, proof)["errors"]


def test_rehashed_certificate_metadata_mismatch_refuses() -> None:
    request = _request()
    proof = _proof(request)
    proof["certificate"]["subject"] = "different"
    assert "CERTIFICATE_HASH_INVALID" in _validate(request, proof)["errors"]


def test_validator_has_no_key_network_or_action_capability() -> None:
    safety = _validate()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
