from __future__ import annotations

import hashlib
import json

import pytest

from scripts.local.phase4lu_keyless_evidence_contract import create_request
from scripts.local.phase4lv_offline_crypto_verifier import (
    make_fixture_policy,
    make_fixture_signature,
    verify_fixture,
)


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


def _policy():
    return make_fixture_policy(
        root_sha256="1" * 64,
        intermediate_sha256="2" * 64,
        subject_sha256="3" * 64,
        issuer="dejoia-local-attestor",
        audience="dejoia-alert-evidence-verifier",
        certificate_policy_sha256="4" * 64,
        public_material_sha256="5" * 64,
    )["policy"]


def _certificate(policy):
    return {
        **{
            key: policy[key]
            for key in (
                "root_sha256",
                "intermediate_sha256",
                "subject_sha256",
                "issuer",
                "audience",
                "algorithm",
                "certificate_policy_sha256",
                "public_material_sha256",
            )
        },
        "not_before": "2026-08-28T19:55:00Z",
        "not_after": "2026-08-28T20:15:00Z",
    }


def _inputs():
    request = _request()
    policy = _policy()
    return (
        request,
        policy,
        _certificate(policy),
        make_fixture_signature(request, policy["public_material_sha256"]),
    )


def _verify(
    request=None,
    policy=None,
    certificate=None,
    signature=None,
    replay=None,
    at="2026-08-28T20:05:00Z",
):
    defaults = _inputs()
    return verify_fixture(
        request or defaults[0],
        policy or defaults[1],
        certificate or defaults[2],
        signature or defaults[3],
        replay or [],
        evaluated_at=at,
    )


def test_fixture_verification_is_deterministic_but_production_refuses() -> None:
    first = _verify()
    assert first == _verify()
    assert first["verdict"] == "PASS" and first["fixture_signature_verified"] is True
    assert first["production_readiness"] == "REFUSE"


@pytest.mark.parametrize(
    "field",
    [
        "root_sha256",
        "intermediate_sha256",
        "subject_sha256",
        "issuer",
        "audience",
        "certificate_policy_sha256",
        "public_material_sha256",
    ],
)
def test_every_trust_binding_is_pinned(field: str) -> None:
    request, policy, certificate, signature = _inputs()
    certificate[field] = "other" if field in {"issuer", "audience"} else "f" * 64
    assert any(
        f"PIN_MISMATCH:{field}" == error
        for error in _verify(request, policy, certificate, signature)["errors"]
    )


def test_policy_hash_and_chain_substitution_refuse() -> None:
    request, policy, certificate, signature = _inputs()
    policy["root_sha256"] = "f" * 64
    assert "POLICY_HASH_MISMATCH" in _verify(request, policy, certificate, signature)["errors"]
    request, policy, certificate, signature = _inputs()
    certificate["intermediate_sha256"] = "e" * 64
    assert (
        "PIN_MISMATCH:intermediate_sha256"
        in _verify(request, policy, certificate, signature)["errors"]
    )


@pytest.mark.parametrize("algorithm", ["RSA-SHA1", "UNKNOWN", "ECDSA-P256-SHA256"])
def test_weak_unknown_and_unimplemented_algorithms_refuse(algorithm: str) -> None:
    request, policy, certificate, signature = _inputs()
    certificate["algorithm"] = algorithm
    signature["algorithm"] = algorithm
    assert _verify(request, policy, certificate, signature)["verdict"] == "REFUSE"


def test_payload_tampering_and_signature_malleability_refuse() -> None:
    request, policy, certificate, signature = _inputs()
    request["audience"] = "broader"
    assert "REQUEST_HASH_MISMATCH" in _verify(request, policy, certificate, signature)["errors"]
    request, policy, certificate, signature = _inputs()
    signature["signature_sha256"] = signature["signature_sha256"].upper()
    assert (
        "SIGNATURE_MALLEABILITY_OR_FORMAT_INVALID"
        in _verify(request, policy, certificate, signature)["errors"]
    )


def test_rehashed_request_cannot_broaden_pinned_identity() -> None:
    request, policy, certificate, signature = _inputs()
    request["audience"] = "broader"
    request["request_sha256"] = _digest(
        {key: value for key, value in request.items() if key != "request_sha256"}
    )
    signature = make_fixture_signature(request, policy["public_material_sha256"])
    assert (
        "REQUEST_AUDIENCE_PIN_MISMATCH"
        in _verify(request, policy, certificate, signature)["errors"]
    )


def test_expired_request_refuses_independently_of_certificate() -> None:
    assert "REQUEST_NOT_CURRENT" in _verify(at="2026-08-28T20:10:00Z")["errors"]


def test_expired_certificate_and_replay_refuse() -> None:
    assert "CERTIFICATE_NOT_CURRENT" in _verify(at="2026-08-28T20:15:00Z")["errors"]
    first = _verify()
    assert "REPLAY_DETECTED" in _verify(replay=[first["verification_identity_sha256"]])["errors"]


def test_malformed_public_material_and_signature_fail_closed() -> None:
    request, policy, certificate, signature = _inputs()
    certificate["public_material_sha256"] = "bad"
    assert _verify(request, policy, certificate, signature)["verdict"] == "REFUSE"
    signature["signature_sha256"] = "bad"
    assert (
        "SIGNATURE_MALLEABILITY_OR_FORMAT_INVALID"
        in _verify(request, policy, certificate, signature)["errors"]
    )


def test_public_fixture_contains_no_private_key_or_operational_authority() -> None:
    request, policy, certificate, signature = _inputs()
    serialized = json.dumps([policy, certificate, signature], sort_keys=True).lower()
    assert "private" not in serialized and "begin" not in serialized
    safety = _verify(request, policy, certificate, signature)["safety"]
    assert safety["offline_only"] is True and safety["fixture_only"] is True
    assert all(
        value is False
        for key, value in safety.items()
        if key not in {"offline_only", "fixture_only"}
    )
