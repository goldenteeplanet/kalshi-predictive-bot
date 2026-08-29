from __future__ import annotations

import copy

from scripts.local.phase4of_quorum_membership_rotation import (
    create_epoch,
    propose_rotation,
    verify_rotation_chain,
)


def _initial():
    return create_epoch(
        epoch=1,
        members={"alpha", "bravo", "charlie"},
        quorum=2,
        revoked=set(),
        parent_epoch_sha256=None,
    )


def _rotation(current=None):
    current = current or _initial()
    return propose_rotation(
        current,
        next_members={"bravo", "charlie", "delta"},
        next_quorum=2,
        revoke={"alpha"},
        old_approvers={"alpha", "bravo"},
        new_approvers={"bravo", "delta"},
    )


def test_overlapping_dual_quorum_rotation_and_chain_pass() -> None:
    first = _initial()
    rotation = _rotation(first)
    assert rotation["verdict"] == "PASS"
    second = rotation["next_epoch"]
    assert second["members"] == ["bravo", "charlie", "delta"]
    assert second["revoked"] == ["alpha"]
    result = verify_rotation_chain([first, second], [rotation])
    assert result["verdict"] == "PASS"


def test_missing_old_new_or_overlap_approval_refuses() -> None:
    first = _initial()
    cases = [
        (
            {"old_approvers": {"alpha"}, "new_approvers": {"bravo", "delta"}},
            "OLD_QUORUM_APPROVAL_MISSING",
        ),
        (
            {"old_approvers": {"alpha", "bravo"}, "new_approvers": {"delta"}},
            "NEW_QUORUM_APPROVAL_MISSING",
        ),
        (
            {"old_approvers": {"alpha", "charlie"}, "new_approvers": {"bravo", "delta"}},
            "QUORUM_OVERLAP_MISSING",
        ),
    ]
    for changed, expected in cases:
        values = {
            "next_members": {"bravo", "charlie", "delta"},
            "next_quorum": 2,
            "revoke": {"alpha"},
            "old_approvers": {"alpha", "bravo"},
            "new_approvers": {"bravo", "delta"},
        }
        values.update(changed)
        result = propose_rotation(first, **values)
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]


def test_revoked_witness_cannot_return_or_approve_later_epoch() -> None:
    first = _initial()
    second = _rotation(first)["next_epoch"]
    result = propose_rotation(
        second,
        next_members={"alpha", "charlie", "delta"},
        next_quorum=2,
        revoke={"bravo"},
        old_approvers={"bravo", "charlie"},
        new_approvers={"alpha", "charlie"},
    )
    assert result["verdict"] == "REFUSE"
    assert "REVOKED_WITNESS_REINTRODUCED" in result["errors"]


def test_forged_hash_parent_epoch_gap_and_revocation_loss_refuse() -> None:
    first = _initial()
    rotation = _rotation(first)
    second = rotation["next_epoch"]
    mutations = []
    forged = copy.deepcopy(second)
    forged["members"].append("mallory")
    mutations.append((forged, "EPOCH_HASH_MISMATCH"))
    wrong_parent = copy.deepcopy(second)
    wrong_parent["parent_epoch_sha256"] = "f" * 64
    mutations.append((wrong_parent, "EPOCH_PARENT_MISMATCH"))
    gap = copy.deepcopy(second)
    gap["epoch"] = 3
    mutations.append((gap, "EPOCH_ROLLBACK_OR_GAP"))
    lost = copy.deepcopy(second)
    lost["revoked"] = []
    mutations.append((lost, "ROTATION_EPOCH_BINDING_INVALID"))
    for mutated, expected in mutations:
        result = verify_rotation_chain([first, mutated], [rotation])
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]


def test_conflicting_rotation_artifact_and_tampered_approval_refuse() -> None:
    first = _initial()
    first_rotation = _rotation(first)
    alternate = propose_rotation(
        first,
        next_members={"alpha", "bravo", "echo"},
        next_quorum=2,
        revoke={"charlie"},
        old_approvers={"alpha", "bravo"},
        new_approvers={"alpha", "echo"},
    )
    result = verify_rotation_chain(
        [first, first_rotation["next_epoch"]], [first_rotation, alternate]
    )
    assert "CHAIN_CARDINALITY_INVALID" in result["errors"]
    assert "CONFLICTING_ROTATIONS" in result["errors"]
    tampered = copy.deepcopy(first_rotation)
    tampered["old_approvers"] = ["alpha"]
    result = verify_rotation_chain([first, first_rotation["next_epoch"]], [tampered])
    assert "ROTATION_HASH_MISMATCH" in result["errors"]


def test_rotation_is_deterministic_input_preserving_and_execution_free() -> None:
    first = _initial()
    original = copy.deepcopy(first)
    one = _rotation(first)
    two = _rotation(first)
    assert one == two
    assert first == original
    assert one["safety"]["offline_only"] is True
    assert all(value is False for key, value in one["safety"].items() if key != "offline_only")
