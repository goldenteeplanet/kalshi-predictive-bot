from __future__ import annotations

import copy

from scripts.local.phase4mu_witness_key_policy import (
    certify_compromise_recovery,
    make_attestation,
    make_key,
    make_policy,
    validate_policy_chain,
    verify_attestations,
)

STORE, HEAD = "a" * 64, "b" * 64


def _keys():
    return [
        make_key(
            "w-a",
            "key-a1",
            "org-a",
            valid_from="2026-08-29T00:00:00Z",
            valid_until="2026-09-29T00:00:00Z",
        ),
        make_key(
            "w-b",
            "key-b1",
            "org-b",
            valid_from="2026-08-29T00:00:00Z",
            valid_until="2026-09-29T00:00:00Z",
        ),
    ]


def _policy(
    version=1, keys=None, threshold=2, previous="0" * 64, effective="2026-08-29T00:00:00Z", **kwargs
):
    return make_policy(
        version=version,
        threshold=threshold,
        keys=keys or _keys(),
        effective_at=effective,
        previous_policy_sha256=previous,
        **kwargs,
    )


def _attest(policy, witness, key, at="2026-08-29T01:00:00Z"):
    return make_attestation(
        witness_id=witness,
        key_id=key,
        policy_sha256=policy["policy_sha256"],
        store_sha256=STORE,
        head_record_sha256=HEAD,
        observed_at=at,
    )


def _verify(policy, statements):
    return verify_attestations(
        statements,
        policy,
        expected_policy_sha256=policy["policy_sha256"],
        expected_store_sha256=STORE,
        expected_head_record_sha256=HEAD,
    )


def test_policy_chain_upgrade_and_pin_are_deterministic() -> None:
    first = _policy()
    second = _policy(
        2, threshold=2, previous=first["policy_sha256"], effective="2026-08-30T00:00:00Z"
    )
    result = validate_policy_chain(
        [first, second], expected_head_policy_sha256=second["policy_sha256"]
    )
    assert result["verdict"] == "PASS"
    assert result == validate_policy_chain(
        [first, second], expected_head_policy_sha256=second["policy_sha256"]
    )
    assert (
        validate_policy_chain([first], expected_head_policy_sha256=second["policy_sha256"])[
            "verdict"
        ]
        == "REFUSE"
    )


def test_downgrade_requires_emergency_evidence() -> None:
    first = _policy(threshold=2)
    weak = _policy(
        2, threshold=1, previous=first["policy_sha256"], effective="2026-08-30T00:00:00Z"
    )
    assert "DOWNGRADE_REQUIRES_EMERGENCY_EVIDENCE" in " ".join(
        validate_policy_chain([first, weak], expected_head_policy_sha256=weak["policy_sha256"])[
            "errors"
        ]
    )
    emergency = _policy(
        2,
        threshold=1,
        previous=first["policy_sha256"],
        effective="2026-08-30T00:00:00Z",
        emergency=True,
        emergency_justification_sha256="c" * 64,
    )
    assert (
        validate_policy_chain(
            [first, emergency], expected_head_policy_sha256=emergency["policy_sha256"]
        )["verdict"]
        == "PASS"
    )


def test_key_activation_expiry_revocation_and_historical_verification() -> None:
    keys = _keys()
    keys[0] = make_key(
        "w-a",
        "key-a1",
        "org-a",
        valid_from="2026-08-29T00:00:00Z",
        valid_until="2026-08-29T03:00:00Z",
        revoked_at="2026-08-29T02:00:00Z",
    )
    policy = _policy(keys=keys)
    before = [
        _attest(policy, "w-a", "key-a1", "2026-08-29T01:00:00Z"),
        _attest(policy, "w-b", "key-b1"),
    ]
    assert _verify(policy, before)["verdict"] == "PASS"
    after = [
        _attest(policy, "w-a", "key-a1", "2026-08-29T02:00:00Z"),
        _attest(policy, "w-b", "key-b1"),
    ]
    assert "KEY_REVOKED" in " ".join(_verify(policy, after)["errors"])
    expired = [
        _attest(policy, "w-a", "key-a1", "2026-08-29T03:00:00Z"),
        _attest(policy, "w-b", "key-b1"),
    ]
    assert "KEY_NOT_ACTIVE" in " ".join(_verify(policy, expired)["errors"])


def test_key_overlap_rotation_allows_old_and_new_only_in_their_windows() -> None:
    keys = _keys() + [
        make_key(
            "w-a",
            "key-a2",
            "org-a",
            valid_from="2026-08-29T01:30:00Z",
            valid_until="2026-10-01T00:00:00Z",
        )
    ]
    policy = _policy(keys=keys)
    old = [_attest(policy, "w-a", "key-a1"), _attest(policy, "w-b", "key-b1")]
    new = [
        _attest(policy, "w-a", "key-a2", "2026-08-29T02:00:00Z"),
        _attest(policy, "w-b", "key-b1"),
    ]
    assert _verify(policy, old)["verdict"] == "PASS"
    assert _verify(policy, new)["verdict"] == "PASS"
    early = [
        _attest(policy, "w-a", "key-a2", "2026-08-29T01:00:00Z"),
        _attest(policy, "w-b", "key-b1"),
    ]
    assert "KEY_NOT_ACTIVE" in " ".join(_verify(policy, early)["errors"])


def test_attestation_replay_across_policy_versions_and_wrong_key_refuses() -> None:
    first = _policy()
    second = _policy(2, previous=first["policy_sha256"], effective="2026-08-30T00:00:00Z")
    replay = [_attest(first, "w-a", "key-a1"), _attest(first, "w-b", "key-b1")]
    assert "POLICY_VERSION_REPLAY" in " ".join(_verify(second, replay)["errors"])
    wrong = [_attest(first, "w-b", "key-a1"), _attest(first, "w-a", "key-a1")]
    assert "KEY_UNKNOWN_OR_WRONG_WITNESS" in " ".join(_verify(first, wrong)["errors"])


def test_compromise_time_invalidates_new_not_historical_attestations() -> None:
    keys = _keys()
    keys[0] = make_key(
        "w-a",
        "key-a1",
        "org-a",
        valid_from="2026-08-29T00:00:00Z",
        valid_until="2026-09-29T00:00:00Z",
        compromised_at="2026-08-29T02:00:00Z",
    )
    policy = _policy(keys=keys)
    historical = [_attest(policy, "w-a", "key-a1"), _attest(policy, "w-b", "key-b1")]
    assert _verify(policy, historical)["verdict"] == "PASS"
    compromised = [
        _attest(policy, "w-a", "key-a1", "2026-08-29T02:00:00Z"),
        _attest(policy, "w-b", "key-b1"),
    ]
    assert "KEY_COMPROMISED" in " ".join(_verify(policy, compromised)["errors"])


def test_compromise_recovery_requires_containment_replacement_and_threshold() -> None:
    old = _policy()
    contained = make_key(
        "w-a",
        "key-a1",
        "org-a",
        valid_from="2026-08-29T00:00:00Z",
        valid_until="2026-09-29T00:00:00Z",
        compromised_at="2026-08-29T02:00:00Z",
    )
    replacement = make_key(
        "w-a-new",
        "key-a2",
        "org-a",
        valid_from="2026-08-29T02:00:00Z",
        valid_until="2026-10-29T00:00:00Z",
    )
    new = _policy(
        2,
        keys=[contained, _keys()[1], replacement],
        previous=old["policy_sha256"],
        effective="2026-08-29T02:00:00Z",
    )
    result = certify_compromise_recovery(old, new, compromised_key_ids=["key-a1"])
    assert result["verdict"] == "PASS"
    assert result["recovery_certified"] is True
    assert result["acceptance_authorized"] is False
    assert result["repair_authorized"] is False
    missing = copy.deepcopy(new)
    missing["keys"].pop()
    assert (
        certify_compromise_recovery(old, missing, compromised_key_ids=["key-a1"])["verdict"]
        == "REFUSE"
    )


def test_policy_and_recovery_have_no_operational_capabilities() -> None:
    result = validate_policy_chain(
        [_policy()], expected_head_policy_sha256=_policy()["policy_sha256"]
    )
    safety = result["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
