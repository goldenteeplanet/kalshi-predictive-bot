"""Offline witness-placement migration and staged cutover proof."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4nu_byzantine_policy import analyze_policy
from scripts.local.phase4nv_witness_placement import audit_placement, effective_independent_count

SCHEMA = "phase4nw.witness-migration.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def build_migration_plan(
    initial_witnesses: list[dict[str, object]],
    replacement_witnesses: list[dict[str, object]],
) -> dict[str, object]:
    actions = []
    for witness in replacement_witnesses:
        witness_id = witness["witness_id"]
        actions.extend(
            [
                {"action": "ADD", "witness": copy.deepcopy(witness)},
                {"action": "ENROLL_KEY", "witness_id": witness_id},
                {"action": "CATCH_UP", "witness_id": witness_id, "checkpoint": "latest-certified"},
                {"action": "WARM_UP", "witness_id": witness_id, "cycles": 3},
                {"action": "DUAL_ATTEST", "witness_id": witness_id},
                {"action": "CERTIFY", "witness_id": witness_id},
            ]
        )
    actions.append(
        {
            "action": "ACTIVATE_POLICY",
            "voters": [row["witness_id"] for row in initial_witnesses + replacement_witnesses],
            "threshold": 5,
            "claimed_byzantine_tolerance": 2,
            "required_independence": 5,
        }
    )
    actions.append({"action": "ROLLBACK_POINT", "name": "expanded-quorum"})
    actions.append(
        {
            "action": "ACTIVATE_POLICY",
            "voters": [row["witness_id"] for row in initial_witnesses[1:] + replacement_witnesses],
            "threshold": 5,
            "claimed_byzantine_tolerance": 1,
            "required_independence": 5,
        }
    )
    actions.append({"action": "REVOKE", "witness_id": initial_witnesses[0]["witness_id"]})
    actions.append(
        {
            "action": "ACTIVATE_POLICY",
            "voters": [initial_witnesses[2]["witness_id"]]
            + [row["witness_id"] for row in replacement_witnesses],
            "threshold": 5,
            "claimed_byzantine_tolerance": 0,
            "required_independence": 5,
        }
    )
    actions.append({"action": "REVOKE", "witness_id": initial_witnesses[1]["witness_id"]})
    actions.append({"action": "ROLLBACK_POINT", "name": "last-old-witness"})
    actions.append(
        {
            "action": "ATOMIC_FINAL_CUTOVER",
            "revoke_witness": initial_witnesses[2]["witness_id"],
            "voters": [row["witness_id"] for row in replacement_witnesses],
            "threshold": 3,
            "claimed_byzantine_tolerance": 1,
            "required_independence": 4,
        }
    )
    body = {
        "schema": SCHEMA,
        "initial_ids": [row["witness_id"] for row in initial_witnesses],
        "replacement_ids": [row["witness_id"] for row in replacement_witnesses],
        "actions": actions,
    }
    return {**body, "plan_sha256": _digest(body)}


def simulate_migration(
    initial_witnesses: list[dict[str, object]],
    replacement_witnesses: list[dict[str, object]],
    actions: list[dict[str, object]],
) -> dict[str, object]:
    errors = []
    state = {
        row["witness_id"]: {
            **copy.deepcopy(row),
            "status": "CERTIFIED",
            "key_enrolled": True,
            "caught_up": True,
            "warmed": True,
            "dual_attested": True,
        }
        for row in initial_witnesses
    }
    policy = {
        "voters": [row["witness_id"] for row in initial_witnesses],
        "threshold": 2,
        "claimed_byzantine_tolerance": 0,
        "required_independence": 1,
    }
    stages = []
    additions = 0
    rollback_points = []
    active_policy_count = 1
    for ordinal, action in enumerate(actions):
        kind = action.get("action")
        stage_errors = []
        if kind == "ADD":
            witness = copy.deepcopy(action["witness"])
            witness_id = witness["witness_id"]
            if witness_id in state:
                stage_errors.append("DUPLICATE_WITNESS_ADD")
            else:
                state[witness_id] = {
                    **witness,
                    "status": "WARMING",
                    "key_enrolled": False,
                    "caught_up": False,
                    "warmed": False,
                    "dual_attested": False,
                }
                additions += 1
        elif kind in {"ENROLL_KEY", "CATCH_UP", "WARM_UP", "DUAL_ATTEST", "CERTIFY"}:
            witness = state.get(action.get("witness_id"))
            if witness is None:
                stage_errors.append("UNKNOWN_MIGRATION_WITNESS")
            elif kind == "ENROLL_KEY":
                key_id = witness.get("key_id")
                if any(
                    other_id != action["witness_id"] and other.get("key_id") == key_id
                    for other_id, other in state.items()
                ):
                    stage_errors.append("SIGNING_KEY_REUSE")
                witness["key_enrolled"] = True
            elif kind == "CATCH_UP":
                if not witness["key_enrolled"] or action.get("checkpoint") != "latest-certified":
                    stage_errors.append("INCOMPLETE_CHECKPOINT_CATCHUP")
                else:
                    witness["caught_up"] = True
            elif kind == "WARM_UP":
                if action.get("cycles", 0) < 3:
                    stage_errors.append("WARMUP_INCOMPLETE")
                else:
                    witness["warmed"] = True
            elif kind == "DUAL_ATTEST":
                if not witness["caught_up"] or not witness["warmed"]:
                    stage_errors.append("DUAL_ATTESTATION_PRECONDITION_FAILED")
                else:
                    witness["dual_attested"] = True
            elif not all(
                witness[field] for field in ("key_enrolled", "caught_up", "warmed", "dual_attested")
            ):
                stage_errors.append("UNCERTIFIED_WITNESS_ACTIVATION")
            else:
                witness["status"] = "CERTIFIED"
        elif kind == "ACTIVATE_POLICY":
            if active_policy_count != 1:
                stage_errors.append("SPLIT_BRAIN_CUTOVER")
            proposed = _policy_from_action(action)
            stage_errors.extend(_validate_policy(proposed, state))
            if not stage_errors:
                policy = proposed
        elif kind == "ROLLBACK_POINT":
            if _validate_policy(policy, state):
                stage_errors.append("LOSS_OF_ROLLBACK_QUORUM")
            else:
                rollback_points.append(
                    {
                        "name": action.get("name"),
                        "ordinal": ordinal,
                        "policy_sha256": _digest(policy),
                        "state_sha256": _digest(state),
                    }
                )
        elif kind == "REVOKE":
            if additions == 0:
                stage_errors.append("REMOVE_BEFORE_ADD")
            witness_id = action.get("witness_id")
            if witness_id in policy["voters"]:
                stage_errors.append("UNSAFE_REVOCATION_ORDER")
            elif witness_id not in state:
                stage_errors.append("UNKNOWN_MIGRATION_WITNESS")
            else:
                state[witness_id]["status"] = "REVOKED"
        elif kind == "ATOMIC_FINAL_CUTOVER":
            proposed = _policy_from_action(action)
            stage_errors.extend(
                _validate_policy(proposed, state, allow_revoke=action.get("revoke_witness"))
            )
            if len(rollback_points) < 2:
                stage_errors.append("LOSS_OF_ROLLBACK_QUORUM")
            if not stage_errors:
                policy = proposed
                state[action["revoke_witness"]]["status"] = "REVOKED"
        elif kind == "DECLARE_SECOND_POLICY":
            active_policy_count += 1
            stage_errors.append("SPLIT_BRAIN_CUTOVER")
        else:
            stage_errors.append("UNKNOWN_MIGRATION_ACTION")
        errors.extend(stage_errors)
        voting = [state[witness_id] for witness_id in policy["voters"] if witness_id in state]
        stages.append(
            {
                "ordinal": ordinal,
                "action": kind,
                "errors": sorted(set(stage_errors)),
                "voter_count": len(voting),
                "threshold": policy["threshold"],
                "effective_independence": effective_independent_count(voting),
                "policy_sha256": _digest(policy),
                "state_sha256": _digest(state),
            }
        )
    final_voters = [state[witness_id] for witness_id in policy["voters"] if witness_id in state]
    final_audit = audit_placement(
        final_voters,
        threshold=policy["threshold"],
        claimed_byzantine_tolerance=policy["claimed_byzantine_tolerance"],
        claimed_independent_witnesses=policy["required_independence"],
    )
    if final_audit["verdict"] != "PASS":
        errors.append("FINAL_STATE_UNVERIFIABLE")
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "stage_count": len(stages),
        "stages": stages,
        "rollback_points": rollback_points,
        "final_policy": policy,
        "final_effective_independence": effective_independent_count(final_voters),
        "final_placement_sha256": final_audit["placement_sha256"],
        "safety": _safety(),
    }
    result["simulation_sha256"] = _digest(result)
    return result


def _policy_from_action(action):
    return {
        "voters": list(action["voters"]),
        "threshold": action["threshold"],
        "claimed_byzantine_tolerance": action["claimed_byzantine_tolerance"],
        "required_independence": action["required_independence"],
    }


def _validate_policy(policy, state, allow_revoke=None):
    errors = []
    voters = []
    for witness_id in policy["voters"]:
        witness = state.get(witness_id)
        if witness is None or witness["status"] != "CERTIFIED":
            errors.append("UNCERTIFIED_WITNESS_ACTIVATION")
        else:
            voters.append(witness)
    if allow_revoke in policy["voters"]:
        errors.append("UNSAFE_REVOCATION_ORDER")
    analysis = analyze_policy(
        len(policy["voters"]),
        policy["threshold"],
        claimed_byzantine_tolerance=policy["claimed_byzantine_tolerance"],
    )
    if analysis["verdict"] != "PASS":
        errors.append("PREMATURE_THRESHOLD_CHANGE")
    if effective_independent_count(voters) < policy["required_independence"]:
        errors.append("EFFECTIVE_INDEPENDENCE_REGRESSION")
    return sorted(set(errors))


def _safety():
    return {
        "offline_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
