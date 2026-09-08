from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from scripts.local.phase4oc_evidence_freshness import (
    BLOCKED_ON_SETTLEMENT,
    issue_freshness_proof,
    renew_freshness_proof,
    verify_freshness_proof,
    verify_renewal_chain,
)

MANIFEST = "a" * 64
START = datetime(2026, 8, 29, 12, tzinfo=UTC)


def _proof():
    return issue_freshness_proof(aggregate_manifest_sha256=MANIFEST, observed_at=START)


def test_fresh_proof_passes_and_boundary_expires_fail_closed() -> None:
    proof = _proof()
    assert (
        verify_freshness_proof(
            proof, trusted_manifest_sha256=MANIFEST, evaluated_at=START + timedelta(hours=5)
        )["verdict"]
        == "PASS"
    )
    expired = verify_freshness_proof(
        proof, trusted_manifest_sha256=MANIFEST, evaluated_at=START + timedelta(hours=6)
    )
    assert expired["verdict"] == "REFUSE"
    assert "EVIDENCE_EXPIRED" in expired["errors"]


def test_future_overlong_and_tampered_proofs_refuse() -> None:
    proof = _proof()
    assert (
        "EVIDENCE_FROM_FUTURE"
        in verify_freshness_proof(
            proof, trusted_manifest_sha256=MANIFEST, evaluated_at=START - timedelta(minutes=6)
        )["errors"]
    )
    overlong = issue_freshness_proof(
        aggregate_manifest_sha256=MANIFEST, observed_at=START, ttl=timedelta(hours=7)
    )
    assert (
        "TTL_OUT_OF_POLICY"
        in verify_freshness_proof(overlong, trusted_manifest_sha256=MANIFEST, evaluated_at=START)[
            "errors"
        ]
    )
    tampered = copy.deepcopy(proof)
    tampered["expires_at"] = (START + timedelta(days=1)).isoformat()
    result = verify_freshness_proof(tampered, trusted_manifest_sha256=MANIFEST, evaluated_at=START)
    assert {"PROOF_HASH_MISMATCH", "EXPIRATION_MISMATCH"} <= set(result["errors"])


def test_manifest_blocker_and_safety_are_anchored() -> None:
    proof = _proof()
    assert proof["blocked_on_september_1_settlement"] == BLOCKED_ON_SETTLEMENT
    wrong_anchor = verify_freshness_proof(
        proof, trusted_manifest_sha256="b" * 64, evaluated_at=START
    )
    assert "TRUSTED_MANIFEST_MISMATCH" in wrong_anchor["errors"]
    mutated = copy.deepcopy(proof)
    mutated["safety"]["paper_order_creation"] = True
    result = verify_freshness_proof(mutated, trusted_manifest_sha256=MANIFEST, evaluated_at=START)
    assert "SAFETY_INVARIANT_VIOLATION" in result["errors"]


def test_renewal_is_deterministic_linked_and_independently_verified() -> None:
    first = _proof()
    renewal_time = START + timedelta(hours=5)
    second = renew_freshness_proof(first, trusted_manifest_sha256=MANIFEST, renewed_at=renewal_time)
    replay = renew_freshness_proof(first, trusted_manifest_sha256=MANIFEST, renewed_at=renewal_time)
    assert second == replay
    assert second["sequence"] == 2
    assert second["parent_proof_sha256"] == first["proof_sha256"]
    result = verify_renewal_chain(
        [first, second],
        trusted_manifest_sha256=MANIFEST,
        evaluated_at=renewal_time + timedelta(hours=1),
    )
    assert result["verdict"] == "PASS"


def test_expired_prior_cannot_be_renewed_and_chain_tampering_refuses() -> None:
    first = _proof()
    with pytest.raises(ValueError, match="not fresh"):
        renew_freshness_proof(
            first, trusted_manifest_sha256=MANIFEST, renewed_at=START + timedelta(hours=6)
        )
    second = renew_freshness_proof(
        first, trusted_manifest_sha256=MANIFEST, renewed_at=START + timedelta(hours=5)
    )
    second["parent_proof_sha256"] = "f" * 64
    result = verify_renewal_chain(
        [first, second],
        trusted_manifest_sha256=MANIFEST,
        evaluated_at=START + timedelta(hours=5, minutes=1),
    )
    assert result["verdict"] == "REFUSE"
    assert "RENEWAL_PARENT_MISMATCH" in result["errors"]


def test_naive_time_and_invalid_initial_link_refuse_creation() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        issue_freshness_proof(aggregate_manifest_sha256=MANIFEST, observed_at=datetime(2026, 8, 29))
    with pytest.raises(ValueError, match="inconsistent"):
        issue_freshness_proof(
            aggregate_manifest_sha256=MANIFEST,
            observed_at=START,
            sequence=2,
            parent_proof_sha256=None,
        )


def test_operations_are_input_preserving_and_execution_free() -> None:
    proof = _proof()
    original = copy.deepcopy(proof)
    result = verify_freshness_proof(proof, trusted_manifest_sha256=MANIFEST, evaluated_at=START)
    assert proof == original
    assert result["safety"]["offline_only"] is True
    assert all(value is False for key, value in result["safety"].items() if key != "offline_only")
