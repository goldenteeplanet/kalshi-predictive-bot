from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4nw_witness_migration import build_migration_plan
from scripts.local.phase4nx_migration_authorization import (
    approve_request,
    consume_simulation_grant,
    create_authorization_request,
    reviewer_registry,
    revoke_approval,
    verify_authorization,
)
from tests.test_phase4nw_witness_migration import _layouts


def _fixture(nonce="nonce-1", epoch=1):
    initial, replacements = _layouts()
    plan = build_migration_plan(initial, replacements)
    source = {"voters": [row["witness_id"] for row in initial], "threshold": 2}
    target = {"voters": [row["witness_id"] for row in replacements], "threshold": 3}
    placement = [f"placement-{row['witness_id']}" for row in replacements]
    rollbacks = ["rollback-expanded", "rollback-last-old"]
    request = create_authorization_request(
        plan,
        stage_start=24,
        stage_end=31,
        source_policy=source,
        target_policy=target,
        witness_identities=[row["witness_id"] for row in initial + replacements],
        placement_evidence_hashes=placement,
        checkpoint_anchor="checkpoint-anchor",
        rollback_point_hashes=rollbacks,
        issued_at="2026-08-29T12:00:00Z",
        expires_at="2026-08-29T14:00:00Z",
        nonce=nonce,
        requester_id="migration-owner",
        authorization_epoch=epoch,
    )
    context = {
        "plan_sha256": plan["plan_sha256"],
        "actions": plan["actions"],
        "source_policy": source,
        "target_policy": target,
        "witness_identities": sorted(row["witness_id"] for row in initial + replacements),
        "placement_evidence_hashes": sorted(placement),
        "checkpoint_anchor": "checkpoint-anchor",
        "rollback_point_hashes": sorted(rollbacks),
        "authorization_epoch": epoch,
        "rolled_back": False,
        "failed_stage": None,
    }
    registry = reviewer_registry()
    approvals = [
        approve_request(
            request, reviewer_id="reviewer-a", approved_at="2026-08-29T12:10:00Z", registry=registry
        ),
        approve_request(
            request, reviewer_id="reviewer-b", approved_at="2026-08-29T12:15:00Z", registry=registry
        ),
    ]
    return plan, request, context, registry, approvals


def _verify(**changes):
    _, request, context, registry, approvals = _fixture()
    return verify_authorization(
        changes.get("request", request),
        changes.get("approvals", approvals),
        registry=registry,
        context=changes.get("context", context),
        now=changes.get("now", "2026-08-29T12:30:00Z"),
        used_authorization_ids=changes.get("used", set()),
        revocations=changes.get("revocations", []),
    )


def test_two_independent_reviewers_authorize_exact_offline_stage_range() -> None:
    result = _verify()
    assert result["verdict"] == "PASS"
    assert result["grant"]["capability"] == "OFFLINE_SIMULATION_ONLY"
    assert result["grant"]["stage_start"] == 24
    assert result["grant"]["stage_end"] == 31


def test_self_duplicate_reused_revoked_and_unauthorized_approvals_refuse() -> None:
    _, request, context, registry, approvals = _fixture()
    duplicate = verify_authorization(
        request,
        [approvals[0], approvals[0]],
        registry=registry,
        context=context,
        now="2026-08-29T12:30:00Z",
    )
    assert "APPROVAL_SIGNATURE_REUSED" in duplicate["errors"]
    self_request = copy.deepcopy(request)
    self_request["requester_id"] = "reviewer-a"
    unsigned = {k: v for k, v in self_request.items() if k != "request_sha256"}
    self_request["request_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    self_approval = approve_request(
        self_request,
        reviewer_id="reviewer-a",
        approved_at="2026-08-29T12:10:00Z",
        registry=registry,
    )
    assert (
        "SELF_APPROVAL_PROHIBITED"
        in verify_authorization(
            self_request,
            [self_approval, approvals[1]],
            registry=registry,
            context=context,
            now="2026-08-29T12:30:00Z",
        )["errors"]
    )
    revocation = revoke_approval(request, approvals[0], reason="review withdrawn")
    assert "APPROVAL_REVOKED" in _verify(revocations=[revocation])["errors"]


def test_plan_policy_witness_placement_checkpoint_and_rollback_drift_refuse() -> None:
    for field, expected in (
        ("plan_sha256", "PLAN_DRIFT"),
        ("source_policy", "SOURCE_POLICY_DRIFT"),
        ("target_policy", "TARGET_POLICY_DRIFT"),
        ("witness_identities", "WITNESS_SUBSTITUTION"),
        ("placement_evidence_hashes", "PLACEMENT_EVIDENCE_DRIFT"),
        ("checkpoint_anchor", "CHECKPOINT_ANCHOR_DRIFT"),
        ("rollback_point_hashes", "ROLLBACK_POINT_DRIFT"),
    ):
        _, _, context, _, _ = _fixture()
        context[field] = "changed"
        assert expected in _verify(context=context)["errors"]


def test_partial_scope_stale_future_and_expired_approval_refuse() -> None:
    _, request, context, registry, approvals = _fixture()
    partial = copy.deepcopy(request)
    partial["stage_actions_sha256"] = "0" * 64
    assert "AUTHORIZATION_REQUEST_INVALID" in _verify(request=partial)["errors"]
    expired = _verify(now="2026-08-29T15:00:00Z")
    assert "AUTHORIZATION_EXPIRED" in expired["errors"]
    future = copy.deepcopy(approvals)
    future[0] = approve_request(
        request, reviewer_id="reviewer-a", approved_at="2026-08-29T13:00:00Z", registry=registry
    )
    assert (
        "APPROVAL_FROM_FUTURE"
        in verify_authorization(
            request, future, registry=registry, context=context, now="2026-08-29T12:30:00Z"
        )["errors"]
    )


def test_authorization_replay_rollback_and_failed_stage_require_reauthorization() -> None:
    valid = _verify()
    authorization_id = valid["authorization_id"]
    assert "AUTHORIZATION_REPLAYED" in _verify(used={authorization_id})["errors"]
    for field, value in (("rolled_back", True), ("failed_stage", 27)):
        _, _, context, _, _ = _fixture()
        context[field] = value
        assert "REAUTHORIZATION_REQUIRED" in _verify(context=context)["errors"]


def test_revocation_and_new_nonce_epoch_support_remediation_reauthorization() -> None:
    _, old_request, _, registry, old_approvals = _fixture()
    revoked = revoke_approval(old_request, old_approvals[0], reason="stage remediation")
    assert "APPROVAL_REVOKED" in _verify(revocations=[revoked])["errors"]
    _, request, context, registry, approvals = _fixture(nonce="nonce-2", epoch=2)
    result = verify_authorization(
        request, approvals, registry=registry, context=context, now="2026-08-29T12:30:00Z"
    )
    assert result["verdict"] == "PASS"
    assert result["authorization_id"] != _verify()["authorization_id"]


def test_grant_is_single_use_and_exact_stage_only() -> None:
    grant = _verify()["grant"]
    consumed = consume_simulation_grant(grant, stage=27, used_authorization_ids=set())
    assert consumed["verdict"] == "PASS"
    used = set(consumed["next_used_authorization_ids"])
    assert (
        "AUTHORIZATION_REPLAYED"
        in consume_simulation_grant(grant, stage=27, used_authorization_ids=used)["errors"]
    )
    assert (
        "STAGE_NOT_AUTHORIZED"
        in consume_simulation_grant(grant, stage=10, used_authorization_ids=set())["errors"]
    )


def test_authorization_layer_has_no_mutation_or_execution_capability() -> None:
    safety = _verify()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
