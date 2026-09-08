from __future__ import annotations

import hashlib
import json

import pytest

from scripts.local.phase4lz_disaster_recovery_simulation import (
    make_incident,
    make_plan,
    simulate_recovery,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _rotation():
    value = {
        "schema": "phase4ly.trust-policy-rotation-validation.v1",
        "fixture_rotation_readiness": "PASS",
        "production_readiness": "REFUSE",
    }
    value["rotation_sha256"] = _digest(value)
    return value


def _capabilities():
    return {
        "policy_activation": False,
        "material_access": False,
        "runtime_control": False,
        "network_access": False,
        "service_control": False,
        "trading": False,
    }


def _inputs(failure_modes=None, affected=None):
    rotation = _rotation()
    plan = make_plan(
        plan_id_sha256="1" * 64,
        rotation_sha256=rotation["rotation_sha256"],
        current_recovery_epoch=7,
        authority_ids=["builder", "scanner", "recovery"],
        custodians=[
            {"custodian_id": "custodian-a", "custodian_sha256": "2" * 64},
            {"custodian_id": "custodian-b", "custodian_sha256": "3" * 64},
        ],
        recovery_media_sha256="4" * 64,
        max_emergency_seconds=1_800,
        required_roles=["BUILDER", "SCANNER", "RECOVERY"],
        required_actions=[
            "REVOKE_COMPROMISED",
            "ROTATE_POLICY",
            "REBUILD_REPLAY_SET",
            "RESTORE_QUORUM",
            "CAPTURE_AFTER_ACTION",
        ],
        capabilities=_capabilities(),
    )
    incident_id = "5" * 64
    approvals = []
    for custodian in ("custodian-a", "custodian-b"):
        approval = {
            "custodian_id": custodian,
            "plan_sha256": plan["plan_sha256"],
            "incident_id_sha256": incident_id,
        }
        approval["approval_sha256"] = _digest(approval)
        approvals.append(approval)
    affected = affected or ["builder"]
    restored_ids = {
        role: f"{role}-new" if role in affected else role
        for role in ("builder", "scanner", "recovery")
    }
    incident = make_incident(
        incident_id_sha256=incident_id,
        plan_sha256=plan["plan_sha256"],
        opened_at="2026-08-28T20:00:00Z",
        emergency_expires_at="2026-08-28T20:30:00Z",
        failure_modes=failure_modes or ["AUTHORITY_COMPROMISE"],
        affected_authority_ids=affected,
        recovery_epoch=8,
        custodian_approvals=approvals,
        recovery_media={"available": True, "media_sha256": "4" * 64, "material_accessed": False},
        clock_evidence={
            "status": "UNAMBIGUOUS",
            "observed_at": "2026-08-28T20:05:00Z",
            "source_count": 2,
            "evidence_sha256": "6" * 64,
        },
        revoked_authority_ids=affected,
        restored_authorities=[
            {
                "authority_id": restored_ids["builder"],
                "role": "BUILDER",
                "authority_sha256": "7" * 64,
                "status": "ACTIVE",
            },
            {
                "authority_id": restored_ids["scanner"],
                "role": "SCANNER",
                "authority_sha256": "8" * 64,
                "status": "ACTIVE",
            },
            {
                "authority_id": restored_ids["recovery"],
                "role": "RECOVERY",
                "authority_sha256": "9" * 64,
                "status": "ACTIVE",
            },
        ],
        rotation_sha256=rotation["rotation_sha256"],
        replay_reconstruction={
            "complete": True,
            "prior_head_sha256": "a" * 64,
            "new_head_sha256": "b" * 64,
            "evidence_sha256": "c" * 64,
        },
        after_action={
            "completed": True,
            "independent_authority_id": "independent-reviewer",
            "evidence_sha256": "d" * 64,
        },
        capabilities=_capabilities(),
    )
    return rotation, plan, incident, []


def _simulate(values=None, at="2026-08-28T20:10:00Z"):
    return simulate_recovery(*(values or _inputs()), evaluated_at=at)


def _rehash_incident(incident):
    incident["incident_sha256"] = _digest(
        {key: value for key, value in incident.items() if key != "incident_sha256"}
    )


def test_single_compromise_recovery_is_deterministic_fixture_pass() -> None:
    first = _simulate()
    assert first == _simulate()
    assert first["fixture_recovery_readiness"] == "PASS"
    assert first["production_readiness"] == "REFUSE"


def test_two_authority_loss_and_replay_cache_loss_can_be_fully_recovered() -> None:
    values = _inputs(["AUTHORITY_LOSS", "REPLAY_CACHE_LOSS"], ["builder", "scanner"])
    assert _simulate(values)["verdict"] == "PASS"


def test_single_person_or_unbound_custody_refuses() -> None:
    values = list(_inputs())
    values[2]["custodian_approvals"] = values[2]["custodian_approvals"][:1]
    _rehash_incident(values[2])
    assert "CUSTODIAN_APPROVALS_INVALID" in _simulate(values)["errors"]
    values = list(_inputs())
    values[2]["custodian_approvals"][0]["plan_sha256"] = "0" * 64
    _rehash_incident(values[2])
    assert "CUSTODIAN_APPROVALS_INVALID" in _simulate(values)["errors"]


def test_unavailable_unpinned_or_accessed_media_refuses() -> None:
    for changes in (
        {"available": False},
        {"media_sha256": "0" * 64},
        {"material_accessed": True},
    ):
        values = list(_inputs())
        values[2]["recovery_media"].update(changes)
        _rehash_incident(values[2])
        assert "RECOVERY_MEDIA_UNAVAILABLE_OR_UNPINNED" in _simulate(values)["errors"]


def test_ambiguous_clock_and_emergency_window_refuse() -> None:
    values = list(_inputs())
    values[2]["clock_evidence"]["status"] = "AMBIGUOUS"
    _rehash_incident(values[2])
    assert "CLOCK_EVIDENCE_AMBIGUOUS_OR_INVALID" in _simulate(values)["errors"]
    assert "EMERGENCY_WINDOW_INVALID" in _simulate(at="2026-08-28T20:30:00Z")["errors"]


@pytest.mark.parametrize("epoch", [7, 9, 0])
def test_recovery_epoch_reuse_skip_and_rollback_refuse(epoch: int) -> None:
    values = list(_inputs())
    values[2]["recovery_epoch"] = epoch
    _rehash_incident(values[2])
    assert "RECOVERY_EPOCH_NOT_MONOTONIC" in _simulate(values)["errors"]


def test_incident_identity_reuse_and_plan_binding_refuse() -> None:
    values = list(_inputs())
    values[3] = [values[2]["incident_id_sha256"]]
    assert "INCIDENT_ID_REUSED" in _simulate(values)["errors"]
    values = list(_inputs())
    values[2]["plan_sha256"] = "0" * 64
    _rehash_incident(values[2])
    assert "INCIDENT_PLAN_BINDING_MISMATCH" in _simulate(values)["errors"]


def test_partial_revocation_or_quorum_restoration_refuses() -> None:
    values = list(_inputs(None, ["builder", "scanner"]))
    values[2]["revoked_authority_ids"] = ["builder"]
    _rehash_incident(values[2])
    assert "REVOCATION_SET_INCOMPLETE" in _simulate(values)["errors"]
    values = list(_inputs())
    values[2]["restored_authorities"] = values[2]["restored_authorities"][:2]
    _rehash_incident(values[2])
    assert "FULL_QUORUM_RESTORATION_INVALID" in _simulate(values)["errors"]


def test_affected_authority_identity_must_be_replaced() -> None:
    values = list(_inputs())
    values[2]["restored_authorities"][0]["authority_id"] = "builder"
    _rehash_incident(values[2])
    assert "AFFECTED_AUTHORITY_NOT_REPLACED" in _simulate(values)["errors"]


def test_missing_rotation_replay_or_after_action_refuses() -> None:
    values = list(_inputs())
    values[2]["rotation_sha256"] = "0" * 64
    values[2]["replay_reconstruction"]["complete"] = False
    values[2]["after_action"]["completed"] = False
    _rehash_incident(values[2])
    errors = _simulate(values)["errors"]
    assert "MANDATORY_ROTATION_BINDING_MISMATCH" in errors
    assert "REPLAY_SET_RECONSTRUCTION_INVALID" in errors
    assert "AFTER_ACTION_EVIDENCE_INVALID" in errors


def test_authority_broadening_and_production_promotion_refuse() -> None:
    values = list(_inputs())
    values[2]["capabilities"]["trading"] = True
    _rehash_incident(values[2])
    assert "INCIDENT_AUTHORITY_BROADENING" in _simulate(values)["errors"]
    assert _simulate()["production_readiness"] == "REFUSE"


def test_simulator_has_no_material_runtime_or_action_capability() -> None:
    safety = _simulate()["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
