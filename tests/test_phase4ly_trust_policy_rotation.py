from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts.local.phase4ly_trust_policy_rotation import (
    make_approval,
    make_policy,
    make_revocation,
    validate_rotation,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _composition():
    value = {
        "schema": "phase4lx.independent-attestation-composition.v1",
        "fixture_readiness": "PASS",
        "production_readiness": "REFUSE",
    }
    value["composition_sha256"] = _digest(value)
    return value


def _authorities(suffix=""):
    return [
        {
            "authority_id": f"builder{suffix}",
            "role": "BUILDER",
            "authority_sha256": "1" * 64,
            "status": "ACTIVE",
        },
        {
            "authority_id": f"scanner{suffix}",
            "role": "SCANNER",
            "authority_sha256": "2" * 64,
            "status": "ACTIVE",
        },
        {
            "authority_id": f"recovery{suffix}",
            "role": "RECOVERY",
            "authority_sha256": "3" * 64,
            "status": "ACTIVE",
        },
    ]


def _policy(epoch, composition, predecessor=None, authorities=None):
    return make_policy(
        epoch=epoch,
        predecessor_policy_sha256=predecessor,
        composition_sha256=composition["composition_sha256"],
        effective_at=f"2026-08-28T{18 + epoch:02d}:00:00Z",
        overlap_until=f"2026-08-28T{18 + epoch:02d}:30:00Z",
        mode="FIXTURE_ONLY",
        quorum={"required_roles": ["BUILDER", "RECOVERY", "SCANNER"], "threshold": 2},
        authorities=authorities or _authorities(),
        capabilities={
            "runtime_control": False,
            "key_access": False,
            "network_access": False,
            "service_control": False,
            "trading": False,
        },
    )


def _inputs(replace_builder=False):
    composition = _composition()
    current = _policy(1, composition)
    authorities = copy.deepcopy(current["authorities"])
    if replace_builder:
        authorities[0] = {
            "authority_id": "builder-new",
            "role": "BUILDER",
            "authority_sha256": "4" * 64,
            "status": "ACTIVE",
        }
    candidate = _policy(2, composition, current["policy_sha256"], authorities)
    common = {
        "current_policy_sha256": current["policy_sha256"],
        "candidate_policy_sha256": candidate["policy_sha256"],
        "issued_at": "2026-08-28T19:30:00Z",
        "expires_at": "2026-08-29T19:30:00Z",
        "self_approval": False,
    }
    approvals = [
        make_approval(
            **common, authority_id="builder", role="BUILDER", replay_identity_sha256="a" * 64
        ),
        make_approval(
            **common, authority_id="scanner", role="SCANNER", replay_identity_sha256="b" * 64
        ),
    ]
    revocations = []
    if replace_builder:
        approvals = [
            make_approval(
                **common,
                authority_id="scanner",
                role="SCANNER",
                replay_identity_sha256="a" * 64,
            ),
            make_approval(
                **common,
                authority_id="recovery",
                role="RECOVERY",
                replay_identity_sha256="b" * 64,
            ),
        ]
        revocations = [
            make_revocation(
                target_authority_id="builder",
                target_authority_sha256="1" * 64,
                reason="COMPROMISE",
                current_policy_sha256=current["policy_sha256"],
                candidate_policy_sha256=candidate["policy_sha256"],
                issued_at="2026-08-28T19:30:00Z",
                expires_at="2026-08-29T19:30:00Z",
                signer_authority_ids=["scanner", "recovery"],
                replay_identity_sha256="c" * 64,
            )
        ]
    return composition, current, candidate, approvals, revocations, []


def _validate(values=None, at="2026-08-28T20:00:00Z"):
    return validate_rotation(*(values or _inputs()), evaluated_at=at)


def _rehash_policy(policy):
    policy["policy_sha256"] = _digest(
        {key: value for key, value in policy.items() if key != "policy_sha256"}
    )


def _rehash_approval(approval):
    approval["approval_sha256"] = _digest(
        {key: value for key, value in approval.items() if key != "approval_sha256"}
    )


def test_no_change_rotation_is_deterministic_fixture_pass_production_refuse() -> None:
    first = _validate()
    assert first == _validate()
    assert first["fixture_rotation_readiness"] == "PASS"
    assert first["production_readiness"] == "REFUSE"


def test_compromise_replacement_with_two_non_target_signers_passes() -> None:
    result = _validate(_inputs(True))
    assert result["verdict"] == "PASS"
    assert result["revocations"][0]["verdict"] == "PASS"


@pytest.mark.parametrize("epoch", [1, 3, 0])
def test_epoch_reuse_skip_and_rollback_refuse(epoch: int) -> None:
    values = list(_inputs())
    values[2]["epoch"] = epoch
    _rehash_policy(values[2])
    assert "EPOCH_ROLLBACK_SKIP_OR_REUSE" in _validate(values)["errors"]


def test_predecessor_and_composition_binding_mismatch_refuse() -> None:
    values = list(_inputs())
    values[2]["predecessor_policy_sha256"] = "0" * 64
    _rehash_policy(values[2])
    assert "PREDECESSOR_BINDING_MISMATCH" in _validate(values)["errors"]
    values = list(_inputs())
    values[2]["composition_sha256"] = "0" * 64
    _rehash_policy(values[2])
    assert "CANDIDATE_COMPOSITION_BINDING_MISMATCH" in _validate(values)["errors"]


def test_quorum_reduction_authority_overlap_and_capability_broadening_refuse() -> None:
    values = list(_inputs())
    values[2]["quorum"]["threshold"] = 1
    values[2]["capabilities"]["trading"] = True
    values[2]["authorities"][1]["authority_sha256"] = values[2]["authorities"][0][
        "authority_sha256"
    ]
    _rehash_policy(values[2])
    errors = _validate(values)["errors"]
    assert any("QUORUM_REDUCTION" in error for error in errors)
    assert any("AUTHORITY_BROADENING" in error for error in errors)
    assert any("DIGEST_INVALID_OR_DUPLICATE" in error for error in errors)


def test_self_approval_duplicate_approver_and_role_quorum_refuse() -> None:
    values = list(_inputs())
    values[3][1].update(authority_id="builder", role="BUILDER", self_approval=True)
    _rehash_approval(values[3][1])
    errors = _validate(values)["errors"]
    assert any("SELF_APPROVAL" in error for error in errors)
    assert any("DUPLICATE_APPROVER" in error for error in errors)
    assert "DISTINCT_ROLE_QUORUM_NOT_MET" in errors


def test_approval_binding_replay_and_staleness_refuse() -> None:
    values = list(_inputs())
    values[3][0].update(candidate_policy_sha256="0" * 64, replay_identity_sha256="f" * 64)
    values[3][1]["replay_identity_sha256"] = "f" * 64
    values[3][0]["expires_at"] = "2026-08-28T19:45:00Z"
    _rehash_approval(values[3][0])
    _rehash_approval(values[3][1])
    errors = _validate(values)["errors"]
    assert any("BINDING_MISMATCH" in error for error in errors)
    assert any("REPLAY_INVALID_OR_DUPLICATE" in error for error in errors)
    assert any("STALE_FUTURE_OR_OVERLONG" in error for error in errors)


def test_removed_authority_requires_exact_bound_revocation() -> None:
    values = list(_inputs(True))
    values[4] = []
    assert "REMOVAL_REVOCATION_SET_MISMATCH" in _validate(values)["errors"]
    values = list(_inputs(True))
    values[4][0]["signer_authority_ids"] = ["builder", "scanner"]
    values[4][0]["revocation_sha256"] = _digest(
        {key: value for key, value in values[4][0].items() if key != "revocation_sha256"}
    )
    assert any("EMERGENCY_SIGNER_QUORUM_INVALID" in error for error in _validate(values)["errors"])


def test_stale_revocation_and_reordered_authorities_refuse() -> None:
    values = list(_inputs(True))
    values[4][0]["expires_at"] = "2026-08-28T19:45:00Z"
    values[4][0]["revocation_sha256"] = _digest(
        {key: value for key, value in values[4][0].items() if key != "revocation_sha256"}
    )
    assert any("STALE_FUTURE_OR_OVERLONG" in error for error in _validate(values)["errors"])
    values = list(_inputs())
    values[2]["authorities"] = list(reversed(values[2]["authorities"]))
    _rehash_policy(values[2])
    assert any("AUTHORITY_ORDER_INVALID" in error for error in _validate(values)["errors"])


def test_overlap_window_and_policy_clock_fail_closed() -> None:
    values = list(_inputs())
    values[2]["overlap_until"] = "2026-08-28T22:30:01Z"
    _rehash_policy(values[2])
    assert "POLICY_TIME_OR_OVERLAP_INVALID" in _validate(values)["errors"]
    assert "POLICY_TIME_INVALID" in _validate(at="not-a-time")["errors"]


def test_fixture_mode_cannot_promote_to_production() -> None:
    values = list(_inputs())
    values[2]["mode"] = "PRODUCTION"
    _rehash_policy(values[2])
    assert any("MODE_INVALID" in error for error in _validate(values)["errors"])


def test_contract_has_no_activation_publication_or_action_capability() -> None:
    safety = _validate()["safety"]
    assert safety["validation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "validation_only")
