"""Independent verifier and mutation campaign for recovery provenance."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4oj_multi_anchor_provenance import SCHEMA as PROVENANCE_SCHEMA

SCHEMA = "phase4ok.provenance-mutation.v1"
MUTATIONS = (
    "CERTIFICATE_VOTE_DELETE",
    "AUTHORITY_IDENTITY_DUPLICATE",
    "GENERATION_DRIFT",
    "JOURNAL_HEAD_DRIFT",
    "SOURCE_ANCHOR_DRIFT",
    "RECONCILIATION_DRIFT",
    "CANONICAL_COPY_DRIFT",
    "STATE_FLIP",
    "CAPABILITY_ENABLE",
    "SAFETY_FLAG_ENABLE",
    "PROVENANCE_HASH_DRIFT",
    "CERTIFICATE_HASH_DRIFT",
    "SCHEMA_DRIFT",
    "ATTESTATION_REORDER",
    "UNKNOWN_FIELD",
    "TYPE_CONFUSION",
    "OVERSIZED_INPUT",
    "VERDICT_FLIP",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def independently_verify_provenance(
    provenance: object,
    certificate: object,
    *,
    trusted_certificate_sha256: str,
    trusted_anchor_sha256: str,
    trusted_reconciliation_sha256: str,
    trusted_canonical_copy_sha256: str,
    maximum_bytes: int,
) -> dict[str, object]:
    errors: list[str] = []
    encoded = json.dumps(
        {"provenance": provenance, "certificate": certificate},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    if len(encoded) > maximum_bytes:
        errors.append("RESOURCE_BOUND_EXCEEDED")
    if not isinstance(provenance, dict) or not isinstance(certificate, dict):
        return _result([*errors, "ENVELOPE_TYPE_INVALID"], len(encoded))
    provenance_fields = {
        "schema",
        "verdict",
        "errors",
        "certificate_sha256",
        "anchor_sha256",
        "reconciliation_sha256",
        "canonical_copy_sha256",
        "state",
        "capabilities_allowed",
        "safety",
        "provenance_sha256",
    }
    if set(provenance) != provenance_fields:
        errors.append("PROVENANCE_FIELDS_INVALID")
    unsigned = {key: value for key, value in provenance.items() if key != "provenance_sha256"}
    if provenance.get("provenance_sha256") != _digest(unsigned):
        errors.append("PROVENANCE_HASH_MISMATCH")
    if provenance.get("schema") != PROVENANCE_SCHEMA:
        errors.append("PROVENANCE_SCHEMA_INVALID")
    if provenance.get("verdict") != "PASS" or provenance.get("errors") != []:
        errors.append("PROVENANCE_NOT_PASSING")
    expected = {
        "certificate_sha256": trusted_certificate_sha256,
        "anchor_sha256": trusted_anchor_sha256,
        "reconciliation_sha256": trusted_reconciliation_sha256,
        "canonical_copy_sha256": trusted_canonical_copy_sha256,
    }
    if any(provenance.get(key) != value for key, value in expected.items()):
        errors.append("EXTERNAL_TRUST_ANCHOR_MISMATCH")
    if provenance.get("state") != "FROZEN" or provenance.get("capabilities_allowed") is not False:
        errors.append("FAIL_CLOSED_STATE_INVALID")
    if provenance.get("safety") != _safety():
        errors.append("SAFETY_INVARIANT_VIOLATION")
    certificate_fields = {
        "schema",
        "verdict",
        "errors",
        "quorum",
        "minimum_generation",
        "allowed_authorities",
        "attestations",
        "equivocators",
        "generation",
        "journal_head_sha256",
        "state",
        "source_anchor_sha256",
        "safety",
        "certificate_sha256",
    }
    if set(certificate) != certificate_fields:
        errors.append("CERTIFICATE_FIELDS_INVALID")
    cert_unsigned = {
        key: value for key, value in certificate.items() if key != "certificate_sha256"
    }
    if certificate.get("certificate_sha256") != _digest(cert_unsigned):
        errors.append("CERTIFICATE_HASH_MISMATCH")
    if certificate.get("certificate_sha256") != trusted_certificate_sha256:
        errors.append("CERTIFICATE_TRUST_ANCHOR_MISMATCH")
    if certificate.get("schema") != PROVENANCE_SCHEMA or certificate.get("verdict") != "PASS":
        errors.append("CERTIFICATE_NOT_PASSING")
    rows = certificate.get("attestations")
    if not isinstance(rows, list):
        errors.append("ATTESTATIONS_TYPE_INVALID")
        rows = []
    identities = [row.get("authority_id") for row in rows if isinstance(row, dict)]
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        errors.append("ATTESTATION_IDENTITY_OR_ORDER_INVALID")
    if len(rows) < int(certificate.get("quorum", 0)):
        errors.append("CERTIFICATE_QUORUM_NOT_MET")
    statement = (
        certificate.get("generation"),
        certificate.get("journal_head_sha256"),
        certificate.get("state"),
        certificate.get("source_anchor_sha256"),
    )
    for row in rows:
        if not isinstance(row, dict):
            errors.append("ATTESTATION_INVALID")
            continue
        row_unsigned = {key: value for key, value in row.items() if key != "attestation_sha256"}
        if row.get("attestation_sha256") != _digest(row_unsigned):
            errors.append("ATTESTATION_HASH_MISMATCH")
        if (
            row.get("generation"),
            row.get("journal_head_sha256"),
            row.get("state"),
            row.get("source_anchor_sha256"),
        ) != statement:
            errors.append("ATTESTATION_STATEMENT_MISMATCH")
    return _result(sorted(set(errors)), len(encoded))


def mutate(provenance: dict[str, object], certificate: dict[str, object], mutation: str):
    p, c = copy.deepcopy(provenance), copy.deepcopy(certificate)
    if mutation == "CERTIFICATE_VOTE_DELETE":
        c["attestations"] = c["attestations"][:1]
    elif mutation == "AUTHORITY_IDENTITY_DUPLICATE":
        c["attestations"][1]["authority_id"] = c["attestations"][0]["authority_id"]
    elif mutation == "GENERATION_DRIFT":
        c["generation"] += 1
    elif mutation == "JOURNAL_HEAD_DRIFT":
        c["journal_head_sha256"] = "1" * 64
    elif mutation == "SOURCE_ANCHOR_DRIFT":
        p["anchor_sha256"] = "2" * 64
    elif mutation == "RECONCILIATION_DRIFT":
        p["reconciliation_sha256"] = "3" * 64
    elif mutation == "CANONICAL_COPY_DRIFT":
        p["canonical_copy_sha256"] = "4" * 64
    elif mutation == "STATE_FLIP":
        p["state"] = "RECOVERED"
    elif mutation == "CAPABILITY_ENABLE":
        p["capabilities_allowed"] = True
    elif mutation == "SAFETY_FLAG_ENABLE":
        p["safety"]["live_execution"] = True
    elif mutation == "PROVENANCE_HASH_DRIFT":
        p["provenance_sha256"] = "5" * 64
        return p, c
    elif mutation == "CERTIFICATE_HASH_DRIFT":
        c["certificate_sha256"] = "6" * 64
        return p, c
    elif mutation == "SCHEMA_DRIFT":
        p["schema"] = "future"
    elif mutation == "ATTESTATION_REORDER":
        c["attestations"] = list(reversed(c["attestations"]))
    elif mutation == "UNKNOWN_FIELD":
        p["unknown"] = True
    elif mutation == "TYPE_CONFUSION":
        c["attestations"] = "not-a-list"
    elif mutation == "OVERSIZED_INPUT":
        p["padding"] = "x" * 1_100_000
    elif mutation == "VERDICT_FLIP":
        p["verdict"] = "REFUSE"
    else:
        raise ValueError(f"unknown mutation: {mutation}")
    p["provenance_sha256"] = _digest(
        {key: value for key, value in p.items() if key != "provenance_sha256"}
    )
    c["certificate_sha256"] = _digest(
        {key: value for key, value in c.items() if key != "certificate_sha256"}
    )
    return p, c


def run_campaign(provenance, certificate, **verifier_kwargs) -> dict[str, object]:
    rows, survivors = [], []
    for mutation_id in MUTATIONS:
        mutated = mutate(provenance, certificate, mutation_id)
        first = independently_verify_provenance(*mutated, **verifier_kwargs)
        second = independently_verify_provenance(*mutated, **verifier_kwargs)
        if first["verdict"] == "PASS":
            survivors.append(mutation_id)
        rows.append(
            {
                "mutation": mutation_id,
                "deterministic": first == second,
                "verdict": first["verdict"],
                "errors": first["errors"],
            }
        )
    errors = []
    if survivors:
        errors.append("MUTATION_SURVIVOR")
    if not all(row["deterministic"] for row in rows):
        errors.append("NONDETERMINISTIC_VERIFICATION")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "mutation_count": len(rows),
        "survivors": survivors,
        "results": rows,
        "independent_of_primary_acceptance": True,
        "safety": _safety(),
    }
    return {**body, "campaign_sha256": _digest(body)}


def _result(errors, encoded_bytes):
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "encoded_bytes": encoded_bytes,
        "safety": _safety(),
    }
    return {**body, "verification_sha256": _digest(body)}


def _safety() -> dict[str, bool]:
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
