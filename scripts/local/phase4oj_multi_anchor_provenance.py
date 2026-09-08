"""Multi-anchor quorum and disaster-recovery provenance verification."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4oi_dual_copy_repair import reconcile_copies

SCHEMA = "phase4oj.multi-anchor-provenance.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def attest_anchor(anchor: dict[str, object], *, authority_id: str) -> dict[str, object]:
    if not authority_id:
        raise ValueError("authority identity is required")
    body = {
        "schema": SCHEMA,
        "authority_id": authority_id,
        "generation": anchor.get("generation"),
        "journal_head_sha256": anchor.get("journal_head_sha256"),
        "state": anchor.get("state"),
        "source_anchor_sha256": anchor.get("anchor_sha256"),
        "safety": _safety(),
    }
    return {**body, "attestation_sha256": _digest(body)}


def certify_anchor_quorum(
    attestations: list[dict[str, object]],
    *,
    allowed_authorities: set[str],
    quorum: int,
    minimum_generation: int,
) -> dict[str, object]:
    errors: list[str] = []
    if quorum < 1 or quorum > len(allowed_authorities):
        errors.append("ANCHOR_QUORUM_POLICY_INVALID")
    identities = [row.get("authority_id") for row in attestations]
    if len(identities) != len(set(identities)):
        errors.append("ANCHOR_AUTHORITY_REPLAY")
    statements: dict[tuple[object, object, object, object], set[str]] = {}
    signed: dict[str, set[tuple[object, object, object, object]]] = {}
    valid_rows = []
    for row in attestations:
        unsigned = {key: value for key, value in row.items() if key != "attestation_sha256"}
        if row.get("attestation_sha256") != _digest(unsigned):
            errors.append("ANCHOR_ATTESTATION_HASH_MISMATCH")
            continue
        authority = str(row.get("authority_id"))
        if authority not in allowed_authorities:
            errors.append("ANCHOR_AUTHORITY_NOT_ALLOWED")
        statement = (
            row.get("generation"),
            row.get("journal_head_sha256"),
            row.get("state"),
            row.get("source_anchor_sha256"),
        )
        statements.setdefault(statement, set()).add(authority)
        signed.setdefault(authority, set()).add(statement)
        valid_rows.append(row)
    equivocators = sorted(authority for authority, values in signed.items() if len(values) > 1)
    if equivocators:
        errors.append("ANCHOR_EQUIVOCATION")
    qualified = [
        (statement, voters) for statement, voters in statements.items() if len(voters) >= quorum
    ]
    if len(qualified) != 1:
        errors.append("ANCHOR_QUORUM_NOT_UNIQUE")
    statement = qualified[0][0] if len(qualified) == 1 else None
    if statement and (not isinstance(statement[0], int) or statement[0] < minimum_generation):
        errors.append("ANCHOR_GENERATION_STALE")
    selected = statement if statement and not errors else None
    rows = sorted(valid_rows, key=lambda row: (str(row["authority_id"]), row["attestation_sha256"]))
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if selected else "REFUSE",
        "errors": sorted(set(errors)),
        "quorum": quorum,
        "minimum_generation": minimum_generation,
        "allowed_authorities": sorted(allowed_authorities),
        "attestations": rows,
        "equivocators": equivocators,
        "generation": selected[0] if selected else None,
        "journal_head_sha256": selected[1] if selected else None,
        "state": selected[2] if selected else "FROZEN",
        "source_anchor_sha256": selected[3] if selected else None,
        "safety": _safety(),
    }
    return {**body, "certificate_sha256": _digest(body)}


def bind_recovery_provenance(
    copies: list[dict[str, object]],
    anchor: dict[str, object],
    certificate: dict[str, object],
) -> dict[str, object]:
    errors = []
    unsigned = {key: value for key, value in certificate.items() if key != "certificate_sha256"}
    if certificate.get("certificate_sha256") != _digest(unsigned):
        errors.append("ANCHOR_CERTIFICATE_HASH_MISMATCH")
    if certificate.get("verdict") != "PASS":
        errors.append("ANCHOR_CERTIFICATE_NOT_PASSING")
    bindings = {
        "generation": anchor.get("generation"),
        "journal_head_sha256": anchor.get("journal_head_sha256"),
        "state": anchor.get("state"),
        "source_anchor_sha256": anchor.get("anchor_sha256"),
    }
    if any(certificate.get(key) != value for key, value in bindings.items()):
        errors.append("ANCHOR_CERTIFICATE_BINDING_MISMATCH")
    reconciliation = reconcile_copies(copies, anchor)
    if reconciliation["verdict"] != "PASS":
        errors.append("DURABLE_COPY_RECONCILIATION_FAILED")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "certificate_sha256": certificate.get("certificate_sha256"),
        "anchor_sha256": anchor.get("anchor_sha256"),
        "reconciliation_sha256": reconciliation["reconciliation_sha256"],
        "canonical_copy_sha256": reconciliation.get("canonical_copy_sha256"),
        "state": reconciliation["state"] if not errors else "FROZEN",
        "capabilities_allowed": bool(not errors and reconciliation.get("capabilities_allowed")),
        "safety": _safety(),
    }
    return {**body, "provenance_sha256": _digest(body)}


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
