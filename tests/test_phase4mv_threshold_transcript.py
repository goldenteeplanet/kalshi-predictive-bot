from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4mu_witness_key_policy import make_key, make_policy
from scripts.local.phase4mv_threshold_transcript import (
    audit_nonce_reuse,
    make_contribution,
    make_transcript,
    resume_after_crash,
    verify_transcript,
)


def _policy(compromise=False):
    keys = [
        make_key(
            "w-a",
            "key-a",
            "org-a",
            valid_from="2026-08-29T00:00:00Z",
            valid_until="2026-09-29T00:00:00Z",
            compromised_at="2026-08-29T02:00:00Z" if compromise else None,
        ),
        make_key(
            "w-b",
            "key-b",
            "org-b",
            valid_from="2026-08-29T00:00:00Z",
            valid_until="2026-09-29T00:00:00Z",
        ),
        make_key(
            "w-c",
            "key-c",
            "org-c",
            valid_from="2026-08-29T00:00:00Z",
            valid_until="2026-09-29T00:00:00Z",
        ),
    ]
    return make_policy(version=1, threshold=2, keys=keys, effective_at="2026-08-29T00:00:00Z")


def _transcript(policy=None, subset=None, round=1, message="d" * 64):
    policy = policy or _policy()
    return make_transcript(
        policy_sha256=policy["policy_sha256"],
        store_sha256="a" * 64,
        head_record_sha256="b" * 64,
        message_sha256=message,
        signing_round=round,
        signer_key_ids=subset or ["key-a", "key-b"],
    )


def _contributions(transcript, at="2026-08-29T01:00:00Z"):
    return [
        make_contribution(
            transcript,
            witness_id="w-a",
            key_id="key-a",
            nonce_commitment_sha256="1" * 64,
            signed_at=at,
        ),
        make_contribution(
            transcript,
            witness_id="w-b",
            key_id="key-b",
            nonce_commitment_sha256="2" * 64,
            signed_at=at,
        ),
    ]


def test_complete_transcript_aggregates_deterministically_and_independently() -> None:
    policy = _policy()
    transcript = _transcript(policy)
    rows = _contributions(transcript)
    first = verify_transcript(transcript, rows, policy)
    assert first == verify_transcript(transcript, rows, policy)
    assert first["verdict"] == "PASS"
    assert first["accepted_contribution_count"] == 2
    assert first["aggregate_sha256"] is not None
    assert first["production_cryptography"] is False


def test_duplicate_signer_nonce_reuse_and_missing_contribution_refuse() -> None:
    policy, transcript = _policy(), _transcript()
    rows = _contributions(transcript)
    duplicate = [rows[0], copy.deepcopy(rows[0])]
    assert "DUPLICATE_SIGNER" in " ".join(
        verify_transcript(transcript, duplicate, policy)["errors"]
    )
    rows[1]["nonce_commitment_sha256"] = rows[0]["nonce_commitment_sha256"]
    rows[1] = make_contribution(
        transcript,
        witness_id="w-b",
        key_id="key-b",
        nonce_commitment_sha256="1" * 64,
        signed_at="2026-08-29T01:00:00Z",
    )
    assert "NONCE_REUSE" in " ".join(verify_transcript(transcript, rows, policy)["errors"])
    assert (
        "CONTRIBUTION_SET_INCOMPLETE_OR_DIFFERENT"
        in verify_transcript(transcript, rows[:1], policy)["errors"]
    )


def test_subset_substitution_mixed_policy_and_transcript_replay_refuse() -> None:
    policy, transcript = _policy(), _transcript()
    rows = _contributions(transcript)
    outsider = make_contribution(
        transcript,
        witness_id="w-c",
        key_id="key-c",
        nonce_commitment_sha256="3" * 64,
        signed_at="2026-08-29T01:00:00Z",
    )
    assert "SIGNER_SUBSET_SUBSTITUTION" in " ".join(
        verify_transcript(transcript, [rows[0], outsider], policy)["errors"]
    )
    changed = _transcript(policy, message="e" * 64)
    assert "TRANSCRIPT_REPLAY_OR_SUBSTITUTION" in " ".join(
        verify_transcript(changed, rows, policy)["errors"]
    )
    rows[0]["policy_sha256"] = "f" * 64
    assert "MIXED_POLICY_VERSION" in " ".join(verify_transcript(transcript, rows, policy)["errors"])


def test_expired_revoked_and_compromised_keys_refuse() -> None:
    policy = _policy(compromise=True)
    transcript = _transcript(policy)
    rows = _contributions(transcript, at="2026-08-29T02:00:00Z")
    assert "KEY_COMPROMISED" in " ".join(verify_transcript(transcript, rows, policy)["errors"])
    policy["keys"][1]["revoked_at"] = "2026-08-29T01:30:00Z"
    key_body = {k: v for k, v in policy["keys"][1].items() if k != "key_record_sha256"}
    policy["keys"][1]["key_record_sha256"] = hashlib.sha256(
        json.dumps(key_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    policy_body = {k: v for k, v in policy.items() if k != "policy_sha256"}
    policy["policy_sha256"] = hashlib.sha256(
        json.dumps(policy_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    transcript = _transcript(policy)
    rows = _contributions(transcript, at="2026-08-29T02:00:00Z")
    assert "KEY_REVOKED" in " ".join(verify_transcript(transcript, rows, policy)["errors"])


def test_partial_round_crash_discards_prepared_and_exact_resume_passes() -> None:
    policy, transcript = _policy(), _transcript()
    rows = _contributions(transcript)
    prepared = make_contribution(
        transcript,
        witness_id="w-b",
        key_id="key-b",
        nonce_commitment_sha256="2" * 64,
        signed_at="2026-08-29T01:00:00Z",
        durability="PREPARED",
    )
    partial = resume_after_crash(transcript, [rows[0], prepared], policy)
    assert partial["verdict"] == "REFUSE"
    assert partial["prepared_discarded"] == 1
    complete = resume_after_crash(transcript, rows, policy)
    assert complete == resume_after_crash(transcript, rows, policy)
    assert complete["verdict"] == "PASS"
    assert complete["durable_contributions_lost"] == 0


def test_cross_transcript_nonce_reuse_is_detected() -> None:
    policy = _policy()
    first = _transcript(policy)
    second = _transcript(policy, round=2)
    rows1, rows2 = _contributions(first), _contributions(second)
    result = audit_nonce_reuse(
        [
            {"transcript": first, "contributions": rows1},
            {"transcript": second, "contributions": rows2},
        ]
    )
    assert result["verdict"] == "REFUSE"
    assert "CROSS_TRANSCRIPT_NONCE_REUSE" in result["errors"]


def test_malformed_context_and_below_threshold_subset_refuse() -> None:
    policy = _policy()
    transcript = _transcript(policy, subset=["key-a"])
    assert "SUBSET_BELOW_THRESHOLD" in verify_transcript(transcript, [], policy)["errors"]
    transcript["message_sha256"] = "f" * 64
    assert "TRANSCRIPT_CONTEXT_INVALID" in verify_transcript(transcript, [], policy)["errors"]


def test_simulation_has_no_cryptographic_or_operational_capability() -> None:
    policy, transcript = _policy(), _transcript()
    result = verify_transcript(transcript, _contributions(transcript), policy)
    safety = result["safety"]
    assert result["production_cryptography"] is False
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
