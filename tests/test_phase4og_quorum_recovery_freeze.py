from __future__ import annotations

import copy

from scripts.local.phase4of_quorum_membership_rotation import create_epoch
from scripts.local.phase4og_quorum_recovery_freeze import (
    adjudicate_recoveries,
    capability_gate,
    create_freeze_certificate,
    recover_membership,
)

AUTHORITIES = {"recovery-1", "recovery-2", "recovery-3", "recovery-4"}


def _epoch():
    return create_epoch(
        epoch=1,
        members={"alpha", "bravo", "charlie"},
        quorum=2,
        revoked=set(),
        parent_epoch_sha256=None,
    )


def _freeze(epoch=None):
    return create_freeze_certificate(epoch or _epoch(), reason="QUORUM_LOSS", observers={"alpha"})


def _recover(epoch=None, freeze=None, **overrides):
    epoch = epoch or _epoch()
    values = {
        "next_members": {"charlie", "delta", "echo"},
        "next_quorum": 2,
        "revoke": {"alpha", "bravo"},
        "recovery_authorities": {"recovery-1", "recovery-2", "recovery-3"},
        "allowed_recovery_authorities": AUTHORITIES,
        "recovery_quorum": 3,
    }
    values.update(overrides)
    return recover_membership(epoch, freeze or _freeze(epoch), **values)


def test_valid_freeze_blocks_time_and_renewal_until_strict_recovery() -> None:
    epoch = _epoch()
    freeze = _freeze(epoch)
    gate = capability_gate(freeze)
    assert gate["verdict"] == "REFUSE"
    assert gate["time_certification_allowed"] is False
    assert gate["freshness_renewal_allowed"] is False
    recovery = _recover(epoch, freeze)
    assert recovery["verdict"] == "PASS"
    assert recovery["state"] == "RECOVERED"
    assert recovery["next_epoch"]["revoked"] == ["alpha", "bravo"]


def test_unilateral_or_nonindependent_recovery_refuses_and_remains_frozen() -> None:
    cases = [
        (
            {"recovery_authorities": {"recovery-1"}},
            "RECOVERY_AUTHORITY_QUORUM_MISSING",
        ),
        (
            {"recovery_authorities": {"recovery-1", "recovery-2", "alpha"}},
            "RECOVERY_AUTHORITY_NOT_INDEPENDENT",
        ),
        ({"recovery_quorum": 2}, "RECOVERY_QUORUM_NOT_STRICTER"),
    ]
    for changed, expected in cases:
        result = _recover(**changed)
        assert result["verdict"] == "REFUSE"
        assert result["state"] == "FROZEN"
        assert expected in result["errors"]


def test_invalid_or_wrong_epoch_freeze_and_revoked_reintroduction_refuse() -> None:
    epoch = _epoch()
    altered = copy.deepcopy(_freeze(epoch))
    altered["reason"] = "forged"
    assert "VALID_FREEZE_REQUIRED" in _recover(epoch, altered)["errors"]
    other = copy.deepcopy(epoch)
    other["epoch_sha256"] = "f" * 64
    assert "VALID_FREEZE_REQUIRED" in _recover(other, _freeze(epoch))["errors"]
    assert (
        "REVOKED_WITNESS_REINTRODUCED"
        in _recover(next_members={"alpha", "delta", "echo"})["errors"]
    )


def test_replayed_and_conflicting_recovery_attempts_fail_closed() -> None:
    epoch = _epoch()
    freeze = _freeze(epoch)
    first = _recover(epoch, freeze)
    replay = adjudicate_recoveries([first, first])
    assert replay["state"] == "FROZEN"
    assert "RECOVERY_REPLAY" in replay["errors"]
    second = _recover(
        epoch,
        freeze,
        next_members={"charlie", "foxtrot", "golf"},
    )
    conflict = adjudicate_recoveries([first, second])
    assert conflict["state"] == "FROZEN"
    assert "CONFLICTING_RECOVERIES" in conflict["errors"]


def test_single_recovery_adjudicates_deterministically_and_execution_free() -> None:
    recovery = _recover()
    original = copy.deepcopy(recovery)
    one = adjudicate_recoveries([recovery])
    two = adjudicate_recoveries([recovery])
    assert one == two
    assert one["verdict"] == "PASS"
    assert recovery == original
    assert one["safety"]["offline_only"] is True
    assert all(value is False for key, value in one["safety"].items() if key != "offline_only")
