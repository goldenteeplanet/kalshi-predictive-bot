"""Create inert signing requests and validate supplied keyless proof metadata offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

REQUEST_SCHEMA = "phase4lu.canonical-evidence-signing-request.v1"
VALIDATION_SCHEMA = "phase4lu.keyless-proof-offline-validation.v1"
PROOF_SCHEMA = "phase4lu.keyless-proof.v1"
EVIDENCE_SCHEMAS = {
    "phase4lr.alert-evidence-export-manifest.v1",
    "phase4ls.offline-verifier-mutation-audit.v1",
    "phase4lt.canonicalization-invariance-audit.v1",
}
ISSUERS = {"dejoia-local-attestor", "dejoia-ci-attestor"}
LOG_IDS = {"dejoia-offline-log-v1"}
AUDIENCE = "dejoia-alert-evidence-verifier"
PURPOSE = "offline-integrity-attestation"
ALGORITHM = "KEYLESS-DSSE-SHA256"
MAX_VALIDITY_SECONDS = 600
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
REQUEST_FIELDS = {
    "schema",
    "evidence_schema",
    "evidence_sha256",
    "purpose",
    "issuer",
    "audience",
    "created_at",
    "expires_at",
    "nonce_sha256",
    "algorithm",
    "transparency_intent",
    "safety",
    "request_sha256",
}
CERTIFICATE_FIELDS = {
    "subject",
    "issuer",
    "audience",
    "not_before",
    "not_after",
    "public_key_algorithm",
    "certificate_sha256",
}
TRANSPARENCY_FIELDS = {
    "log_id",
    "leaf_sha256",
    "root_sha256",
    "tree_size",
    "leaf_index",
    "inclusion_path",
}
PROOF_FIELDS = {"schema", "request_sha256", "certificate", "signature", "transparency"}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _node(left: str, right: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def create_request(
    *,
    evidence_schema: str,
    evidence_sha256: str,
    issuer: str,
    created_at: str,
    expires_at: str,
    raw_nonce: str,
) -> dict[str, object]:
    errors: list[str] = []
    created = _time(created_at)
    expires = _time(expires_at)
    if evidence_schema not in EVIDENCE_SCHEMAS:
        errors.append("EVIDENCE_SCHEMA_NOT_ALLOWLISTED")
    if HEX64.fullmatch(evidence_sha256) is None:
        errors.append("EVIDENCE_HASH_INVALID")
    if issuer not in ISSUERS:
        errors.append("ISSUER_NOT_ALLOWLISTED")
    if created is None or expires is None:
        errors.append("REQUEST_TIME_INVALID")
    elif expires <= created or (expires - created).total_seconds() > MAX_VALIDITY_SECONDS:
        errors.append("REQUEST_VALIDITY_INVALID")
    if not isinstance(raw_nonce, str) or not 16 <= len(raw_nonce) <= 256:
        errors.append("NONCE_INVALID")
    request: dict[str, object] = {
        "schema": REQUEST_SCHEMA,
        "evidence_schema": evidence_schema,
        "evidence_sha256": evidence_sha256,
        "purpose": PURPOSE,
        "issuer": issuer,
        "audience": AUDIENCE,
        "created_at": created_at,
        "expires_at": expires_at,
        "nonce_sha256": hashlib.sha256(raw_nonce.encode()).hexdigest(),
        "algorithm": ALGORITHM,
        "transparency_intent": "RECORD_REQUIRED_OFFLINE_VERIFICATION",
        "safety": {
            "inert_request": True,
            "raw_nonce_included": False,
            "private_key_included": False,
            "credential_included": False,
            "network_endpoint_included": False,
            "signing_capability": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    request["request_sha256"] = _digest(request)
    return {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "request": request if not errors else None,
    }


def _request_errors(request: object, evaluated_at: str) -> list[str]:
    if not isinstance(request, dict) or set(request) != REQUEST_FIELDS:
        return ["REQUEST_FIELD_SET_INVALID"]
    errors: list[str] = []
    body = {key: value for key, value in request.items() if key != "request_sha256"}
    if request.get("request_sha256") != _digest(body):
        errors.append("REQUEST_HASH_MISMATCH")
    if request.get("evidence_schema") not in EVIDENCE_SCHEMAS:
        errors.append("EVIDENCE_SCHEMA_NOT_ALLOWLISTED")
    if HEX64.fullmatch(str(request.get("evidence_sha256"))) is None:
        errors.append("EVIDENCE_HASH_INVALID")
    if HEX64.fullmatch(str(request.get("nonce_sha256"))) is None:
        errors.append("NONCE_DIGEST_INVALID")
    if request.get("issuer") not in ISSUERS:
        errors.append("ISSUER_NOT_ALLOWLISTED")
    if request.get("audience") != AUDIENCE or request.get("purpose") != PURPOSE:
        errors.append("REQUEST_AUTHORITY_BINDING_INVALID")
    if request.get("algorithm") != ALGORITHM:
        errors.append("ALGORITHM_NOT_ALLOWLISTED")
    if request.get("transparency_intent") != "RECORD_REQUIRED_OFFLINE_VERIFICATION":
        errors.append("TRANSPARENCY_INTENT_INVALID")
    expected_safety = {
        "inert_request": True,
        "raw_nonce_included": False,
        "private_key_included": False,
        "credential_included": False,
        "network_endpoint_included": False,
        "signing_capability": False,
        "network_access": False,
        "order_capability": False,
    }
    if request.get("safety") != expected_safety:
        errors.append("REQUEST_SAFETY_INVALID")
    now = _time(evaluated_at)
    created = _time(request.get("created_at"))
    expires = _time(request.get("expires_at"))
    if now is None or created is None or expires is None:
        errors.append("CLOCK_EVIDENCE_INVALID")
    else:
        if expires <= created or (expires - created).total_seconds() > MAX_VALIDITY_SECONDS:
            errors.append("REQUEST_VALIDITY_INVALID")
        if now < created or now >= expires:
            errors.append("REQUEST_NOT_CURRENT")
    return errors


def validate_proof(
    request: object,
    proof: object,
    replay_set: object,
    *,
    evaluated_at: str,
) -> dict[str, object]:
    errors = _request_errors(request, evaluated_at)
    source = request if isinstance(request, dict) else {}
    if not isinstance(replay_set, list) or not all(isinstance(item, str) for item in replay_set):
        errors.append("REPLAY_SET_INVALID")
        replay_set = []
    if not isinstance(proof, dict) or set(proof) != PROOF_FIELDS:
        errors.append("PROOF_FIELD_SET_INVALID")
        proof = {}
    if proof.get("schema") != PROOF_SCHEMA:
        errors.append("PROOF_SCHEMA_INVALID")
    if proof.get("request_sha256") != source.get("request_sha256"):
        errors.append("PROOF_REQUEST_BINDING_MISMATCH")
    certificate = proof.get("certificate")
    if not isinstance(certificate, dict) or set(certificate) != CERTIFICATE_FIELDS:
        errors.append("CERTIFICATE_FIELD_SET_INVALID")
        certificate = {}
    if (
        certificate.get("issuer") != source.get("issuer")
        or certificate.get("issuer") not in ISSUERS
    ):
        errors.append("CERTIFICATE_ISSUER_MISMATCH")
    if certificate.get("audience") != AUDIENCE:
        errors.append("CERTIFICATE_AUDIENCE_MISMATCH")
    if certificate.get("subject") != "dejoia-alert-evidence-builder":
        errors.append("CERTIFICATE_SUBJECT_MISMATCH")
    if certificate.get("public_key_algorithm") != "ECDSA-P256-SHA256":
        errors.append("CERTIFICATE_ALGORITHM_INVALID")
    now = _time(evaluated_at)
    not_before = _time(certificate.get("not_before"))
    not_after = _time(certificate.get("not_after"))
    if now is None or not_before is None or not_after is None:
        errors.append("CERTIFICATE_TIME_INVALID")
    elif not_before > now or now >= not_after or (not_after - not_before).total_seconds() > 3_600:
        errors.append("CERTIFICATE_VALIDITY_INVALID")
    certificate_body = {
        key: value for key, value in certificate.items() if key != "certificate_sha256"
    }
    if certificate.get("certificate_sha256") != _digest(certificate_body):
        errors.append("CERTIFICATE_HASH_INVALID")
    signature = proof.get("signature")
    if not isinstance(signature, dict) or set(signature) != {
        "algorithm",
        "payload_sha256",
        "signature_sha256",
    }:
        errors.append("SIGNATURE_FIELD_SET_INVALID")
        signature = {}
    if signature.get("algorithm") != ALGORITHM:
        errors.append("SIGNATURE_ALGORITHM_INVALID")
    if signature.get("payload_sha256") != source.get("request_sha256"):
        errors.append("SIGNATURE_PAYLOAD_MISMATCH")
    if HEX64.fullmatch(str(signature.get("signature_sha256"))) is None:
        errors.append("SIGNATURE_DIGEST_INVALID")
    transparency = proof.get("transparency")
    if not isinstance(transparency, dict) or set(transparency) != TRANSPARENCY_FIELDS:
        errors.append("TRANSPARENCY_FIELD_SET_INVALID")
        transparency = {}
    if transparency.get("log_id") not in LOG_IDS:
        errors.append("TRANSPARENCY_LOG_NOT_ALLOWLISTED")
    expected_leaf = _digest(
        {
            "request_sha256": source.get("request_sha256"),
            "certificate_sha256": certificate.get("certificate_sha256"),
            "signature_sha256": signature.get("signature_sha256"),
        }
    )
    if transparency.get("leaf_sha256") != expected_leaf:
        errors.append("TRANSPARENCY_LEAF_MISMATCH")
    path = transparency.get("inclusion_path")
    computed = expected_leaf
    if not isinstance(path, list) or len(path) > 64:
        errors.append("INCLUSION_PATH_INVALID")
        path = []
    for index, item in enumerate(path):
        if (
            not isinstance(item, dict)
            or set(item) != {"side", "sha256"}
            or item.get("side") not in {"left", "right"}
            or HEX64.fullmatch(str(item.get("sha256"))) is None
        ):
            errors.append(f"INCLUSION_PATH_{index}:INVALID")
            continue
        computed = (
            _node(item["sha256"], computed)
            if item["side"] == "left"
            else _node(computed, item["sha256"])
        )
    if transparency.get("root_sha256") != computed:
        errors.append("INCLUSION_ROOT_MISMATCH")
    tree_size = transparency.get("tree_size")
    leaf_index = transparency.get("leaf_index")
    if (
        type(tree_size) is not int
        or type(leaf_index) is not int
        or tree_size < 1
        or not 0 <= leaf_index < tree_size
    ):
        errors.append("TRANSPARENCY_POSITION_INVALID")
    if tree_size == 1 and path:
        errors.append("INCLUSION_PATH_LENGTH_INVALID")
    proof_identity = _digest(proof)
    if proof_identity in replay_set or source.get("nonce_sha256") in replay_set:
        errors.append("REPLAY_DETECTED")
    result: dict[str, object] = {
        "schema": VALIDATION_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "request_sha256": source.get("request_sha256"),
        "proof_sha256": proof_identity,
        "merkle_inclusion_structurally_verified": "INCLUSION_ROOT_MISMATCH" not in errors,
        "cryptographic_signature_verified": False,
        "limitations": ["SIGNATURE_BYTES_AND_PUBLIC_KEY_NOT_ACCEPTED_BY_THIS_CONTRACT"],
        "safety": {
            "offline_only": True,
            "key_generation": False,
            "key_import": False,
            "signature_operation": False,
            "network_access": False,
            "credential_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["validation_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("proof")
    parser.add_argument("replay_set")
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    with open(args.request, encoding="utf-8") as stream:
        request = json.load(stream)
    with open(args.proof, encoding="utf-8") as stream:
        proof = json.load(stream)
    with open(args.replay_set, encoding="utf-8") as stream:
        replay_set = json.load(stream)
    result = validate_proof(request, proof, replay_set, evaluated_at=args.evaluated_at)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
