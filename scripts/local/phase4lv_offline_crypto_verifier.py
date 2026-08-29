"""Offline fixture-verifier interface with explicit trust-root pinning."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
from datetime import datetime

SCHEMA = "phase4lv.offline-cryptographic-verification.v1"
TRUST_SCHEMA = "phase4lv.trust-root-pinning-policy.v1"
REQUEST_SCHEMA = "phase4lu.canonical-evidence-signing-request.v1"
FIXTURE_ALGORITHM = "FIXTURE-SHA256-PUBLIC-BINDING-V1"
PRODUCTION_ALGORITHMS: set[str] = set()
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
POLICY_FIELDS = {
    "schema",
    "mode",
    "root_sha256",
    "intermediate_sha256",
    "subject_sha256",
    "issuer",
    "audience",
    "algorithm",
    "certificate_policy_sha256",
    "public_material_sha256",
    "trust_policy_sha256",
}
CERT_FIELDS = {
    "root_sha256",
    "intermediate_sha256",
    "subject_sha256",
    "issuer",
    "audience",
    "algorithm",
    "certificate_policy_sha256",
    "public_material_sha256",
    "not_before",
    "not_after",
}
SIGNATURE_FIELDS = {"algorithm", "payload_sha256", "signature_sha256", "encoding"}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def make_fixture_policy(**pins: str) -> dict[str, object]:
    required = {
        "root_sha256",
        "intermediate_sha256",
        "subject_sha256",
        "issuer",
        "audience",
        "certificate_policy_sha256",
        "public_material_sha256",
    }
    errors: list[str] = []
    if set(pins) != required:
        errors.append("POLICY_INPUT_FIELD_SET_INVALID")
    for field in required - {"issuer", "audience"}:
        if HEX64.fullmatch(str(pins.get(field))) is None:
            errors.append(f"POLICY_PIN_INVALID:{field}")
    if pins.get("issuer") not in {"dejoia-local-attestor", "dejoia-ci-attestor"}:
        errors.append("POLICY_ISSUER_INVALID")
    if pins.get("audience") != "dejoia-alert-evidence-verifier":
        errors.append("POLICY_AUDIENCE_INVALID")
    policy: dict[str, object] = {
        "schema": TRUST_SCHEMA,
        "mode": "TEST_FIXTURE_ONLY",
        **pins,
        "algorithm": FIXTURE_ALGORITHM,
    }
    policy["trust_policy_sha256"] = _digest(policy)
    return {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "policy": policy if not errors else None,
    }


def make_fixture_signature(
    request: dict[str, object], public_material_sha256: str
) -> dict[str, str]:
    payload = _canonical(request)
    payload_hash = hashlib.sha256(payload).hexdigest()
    signature = hashlib.sha256(
        b"phase4lv-fixture-signature-v1\x00" + bytes.fromhex(public_material_sha256) + payload
    ).hexdigest()
    return {
        "algorithm": FIXTURE_ALGORITHM,
        "payload_sha256": payload_hash,
        "signature_sha256": signature,
        "encoding": "LOWERCASE_HEX_FIXED_64",
    }


def verify_fixture(
    request: object,
    policy: object,
    certificate: object,
    signature: object,
    replay_set: object,
    *,
    evaluated_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA:
        errors.append("REQUEST_INVALID")
        request = {}
    else:
        request_body = {key: value for key, value in request.items() if key != "request_sha256"}
        if request.get("request_sha256") != _digest(request_body):
            errors.append("REQUEST_HASH_MISMATCH")
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        errors.append("POLICY_FIELD_SET_INVALID")
        policy = {}
    else:
        policy_body = {key: value for key, value in policy.items() if key != "trust_policy_sha256"}
        if policy.get("trust_policy_sha256") != _digest(policy_body):
            errors.append("POLICY_HASH_MISMATCH")
    if policy.get("mode") != "TEST_FIXTURE_ONLY":
        errors.append("POLICY_MODE_INVALID")
    if policy.get("algorithm") != FIXTURE_ALGORITHM:
        errors.append("ALGORITHM_NOT_ALLOWLISTED")
    if request.get("issuer") != policy.get("issuer"):
        errors.append("REQUEST_ISSUER_PIN_MISMATCH")
    if request.get("audience") != policy.get("audience"):
        errors.append("REQUEST_AUDIENCE_PIN_MISMATCH")
    now = _time(evaluated_at)
    request_created = _time(request.get("created_at"))
    request_expires = _time(request.get("expires_at"))
    if now is None or request_created is None or request_expires is None:
        errors.append("REQUEST_CLOCK_INVALID")
    elif now < request_created or now >= request_expires:
        errors.append("REQUEST_NOT_CURRENT")
    if not isinstance(certificate, dict) or set(certificate) != CERT_FIELDS:
        errors.append("CERTIFICATE_FIELD_SET_INVALID")
        certificate = {}
    for field in (
        "root_sha256",
        "intermediate_sha256",
        "subject_sha256",
        "issuer",
        "audience",
        "algorithm",
        "certificate_policy_sha256",
        "public_material_sha256",
    ):
        if certificate.get(field) != policy.get(field):
            errors.append(f"PIN_MISMATCH:{field}")
    if certificate.get("algorithm") != FIXTURE_ALGORITHM:
        errors.append("CERTIFICATE_ALGORITHM_INVALID")
    not_before = _time(certificate.get("not_before"))
    not_after = _time(certificate.get("not_after"))
    if now is None or not_before is None or not_after is None:
        errors.append("CERTIFICATE_CLOCK_INVALID")
    elif not_before > now or now >= not_after or (not_after - not_before).total_seconds() > 3_600:
        errors.append("CERTIFICATE_NOT_CURRENT")
    if not isinstance(signature, dict) or set(signature) != SIGNATURE_FIELDS:
        errors.append("SIGNATURE_FIELD_SET_INVALID")
        signature = {}
    if signature.get("algorithm") != FIXTURE_ALGORITHM:
        errors.append("SIGNATURE_ALGORITHM_INVALID")
    if signature.get("encoding") != "LOWERCASE_HEX_FIXED_64":
        errors.append("SIGNATURE_ENCODING_INVALID")
    supplied_signature = signature.get("signature_sha256")
    if HEX64.fullmatch(str(supplied_signature)) is None:
        errors.append("SIGNATURE_MALLEABILITY_OR_FORMAT_INVALID")
    payload = _canonical(request)
    payload_hash = hashlib.sha256(payload).hexdigest()
    if signature.get("payload_sha256") != payload_hash:
        errors.append("SIGNATURE_PAYLOAD_MISMATCH")
    public_material_hash = certificate.get("public_material_sha256")
    expected_signature = None
    if HEX64.fullmatch(str(public_material_hash)) is not None:
        expected_signature = hashlib.sha256(
            b"phase4lv-fixture-signature-v1\x00" + bytes.fromhex(public_material_hash) + payload
        ).hexdigest()
    if expected_signature is None or not hmac.compare_digest(
        str(supplied_signature), expected_signature
    ):
        errors.append("FIXTURE_SIGNATURE_INVALID")
    if not isinstance(replay_set, list) or not all(isinstance(item, str) for item in replay_set):
        errors.append("REPLAY_SET_INVALID")
        replay_set = []
    verification_identity = _digest(
        {
            "request_sha256": request.get("request_sha256"),
            "trust_policy_sha256": policy.get("trust_policy_sha256"),
            "signature_sha256": supplied_signature,
        }
    )
    if verification_identity in replay_set or request.get("nonce_sha256") in replay_set:
        errors.append("REPLAY_DETECTED")
    fixture_verified = not errors
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if fixture_verified else "REFUSE",
        "errors": sorted(set(errors)),
        "fixture_signature_verified": fixture_verified,
        "production_readiness": "REFUSE",
        "production_reasons": [
            "NO_APPROVED_PRODUCTION_PUBLIC_KEY_BACKEND",
            "NO_PRODUCTION_TRUST_ROOTS_PINNED",
            "FIXTURE_ALGORITHM_NOT_PRODUCTION_CRYPTOGRAPHY",
        ],
        "request_sha256": request.get("request_sha256"),
        "trust_policy_sha256": policy.get("trust_policy_sha256"),
        "verification_identity_sha256": verification_identity,
        "safety": {
            "offline_only": True,
            "fixture_only": True,
            "os_trust_store_access": False,
            "private_key_access": False,
            "key_generation": False,
            "key_import": False,
            "network_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["verification_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("policy")
    parser.add_argument("certificate")
    parser.add_argument("signature")
    parser.add_argument("replay_set")
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    values = []
    for path in (args.request, args.policy, args.certificate, args.signature, args.replay_set):
        with open(path, encoding="utf-8") as stream:
            values.append(json.load(stream))
    result = verify_fixture(*values, evaluated_at=args.evaluated_at)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
