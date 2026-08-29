"""Audit Phase 4MB repair plans for mutation and capability escalation."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections.abc import Callable

from scripts.local.phase4ma_recovery_state_machine import STATES
from scripts.local.phase4mb_checkpoint_repair_planner import SCHEMA as PLAN_SCHEMA

SCHEMA = "phase4mc.repair-plan-mutation-audit.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
TOP_LEVEL_FIELDS = {
    "schema",
    "verdict",
    "errors",
    "source_checkpoints_sha256",
    "source_unchanged",
    "manifest_sha256",
    "trusted_prefix_count",
    "trusted_checkpoint_sha256",
    "corruption_boundary_index",
    "boundary_errors",
    "actions",
    "minimality",
    "safety",
    "plan_sha256",
}
MINIMALITY = {
    "action_count",
    "duplicate_actions",
    "source_edit_authorized",
    "hash_invention_authorized",
    "revalidation_skip_authorized",
}
SAFETY = {
    "planning_only",
    "checkpoint_write",
    "repair_execution",
    "policy_activation",
    "runtime_write",
    "wsl_control",
    "service_control",
    "network_access",
    "order_capability",
}
ACTION_FIELDS = {
    "ABORT_RECOVERY": {"action", "reason"},
    "NO_REPAIR": {"action", "reason"},
    "DISCARD_SUFFIX": {"action", "from_source_index", "reason"},
    "REACQUIRE_EVIDENCE": {"action", "from_state", "reason"},
    "REPLAY_FROM_CHECKPOINT": {"action", "checkpoint_sha256", "checkpoint_state"},
    "REBUILD_RESUME_TOKEN": {"action", "checkpoint_sha256", "revalidate_invariants"},
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _body_hash(plan: dict[str, object]) -> str:
    return _digest({key: value for key, value in plan.items() if key != "plan_sha256"})


def plan_errors(plan: object) -> list[str]:
    if not isinstance(plan, dict):
        return ["PLAN_NOT_OBJECT"]
    errors: list[str] = []
    if set(plan) != TOP_LEVEL_FIELDS:
        errors.append("FIELD_SET_INVALID")
    if plan.get("schema") != PLAN_SCHEMA:
        errors.append("SCHEMA_INVALID")
    if plan.get("verdict") not in {"PASS", "REFUSE"}:
        errors.append("VERDICT_INVALID")
    for field in ("source_checkpoints_sha256", "manifest_sha256", "plan_sha256"):
        if HEX64.fullmatch(str(plan.get(field))) is None:
            errors.append(f"{field.upper()}_INVALID")
    if plan.get("plan_sha256") != _body_hash(plan):
        errors.append("PLAN_HASH_MISMATCH")
    trusted_count = plan.get("trusted_prefix_count")
    if type(trusted_count) is not int or not 0 <= trusted_count <= len(STATES):
        errors.append("TRUSTED_PREFIX_INVALID")
    trusted_hash = plan.get("trusted_checkpoint_sha256")
    if trusted_count == 0:
        if trusted_hash is not None:
            errors.append("TRUSTED_CHECKPOINT_INVALID")
    elif HEX64.fullmatch(str(trusted_hash)) is None:
        errors.append("TRUSTED_CHECKPOINT_INVALID")
    boundary = plan.get("corruption_boundary_index")
    if boundary is not None and (type(boundary) is not int or boundary < 0):
        errors.append("BOUNDARY_INVALID")
    for field in ("errors", "boundary_errors"):
        value = plan.get(field)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            errors.append(f"{field.upper()}_INVALID")
    minimality = plan.get("minimality")
    if not isinstance(minimality, dict) or set(minimality) != MINIMALITY:
        errors.append("MINIMALITY_INVALID")
    else:
        if type(minimality.get("action_count")) is not int:
            errors.append("ACTION_COUNT_INVALID")
        for field in MINIMALITY - {"action_count"}:
            if type(minimality.get(field)) is not bool:
                errors.append("MINIMALITY_TYPE_INVALID")
        if any(
            minimality.get(field) is not False
            for field in (
                "duplicate_actions",
                "source_edit_authorized",
                "hash_invention_authorized",
                "revalidation_skip_authorized",
            )
        ):
            errors.append("MINIMALITY_ESCALATION")
    safety = plan.get("safety")
    if not isinstance(safety, dict) or set(safety) != SAFETY:
        errors.append("SAFETY_INVALID")
    elif safety.get("planning_only") is not True or any(
        safety.get(field) is not False for field in SAFETY - {"planning_only"}
    ):
        errors.append("CAPABILITY_ESCALATION")
    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("ACTIONS_INVALID")
        actions = []
    action_names: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            errors.append("ACTION_NOT_OBJECT")
            continue
        name = action.get("action")
        action_names.append(str(name))
        if name not in ACTION_FIELDS or set(action) != ACTION_FIELDS.get(name, set()):
            errors.append("ACTION_SCHEMA_INVALID")
        if (
            name in {"REPLAY_FROM_CHECKPOINT", "REBUILD_RESUME_TOKEN"}
            and HEX64.fullmatch(str(action.get("checkpoint_sha256"))) is None
        ):
            errors.append("ACTION_CHECKPOINT_HASH_INVALID")
        if name == "REACQUIRE_EVIDENCE" and action.get("from_state") not in STATES:
            errors.append("REACQUIRE_STATE_INVALID")
        if name == "REBUILD_RESUME_TOKEN" and action.get("revalidate_invariants") is not True:
            errors.append("REVALIDATION_SUPPRESSED")
    if isinstance(minimality, dict) and minimality.get("action_count") != len(actions):
        errors.append("ACTION_COUNT_MISMATCH")
    if len(action_names) != len(set(action_names)):
        errors.append("DUPLICATE_ACTION")
    if "REACQUIRE_EVIDENCE" in action_names and not {
        "REPLAY_FROM_CHECKPOINT",
        "REBUILD_RESUME_TOKEN",
    }.issubset(action_names):
        errors.append("MANDATORY_REPAIR_ACTION_MISSING")
    return sorted(set(errors))


def audit_candidate(baseline: object, candidate: object) -> dict[str, object]:
    baseline_errors = plan_errors(baseline)
    candidate_errors = plan_errors(candidate)
    binding_errors: list[str] = []
    if not baseline_errors and isinstance(baseline, dict) and isinstance(candidate, dict):
        immutable = TOP_LEVEL_FIELDS - {"plan_sha256"}
        for field in sorted(immutable):
            if candidate.get(field) != baseline.get(field):
                binding_errors.append(f"BASELINE_{field.upper()}_MISMATCH")
        if candidate.get("trusted_prefix_count", -1) > baseline.get("trusted_prefix_count", -1):
            binding_errors.append("TRUST_PREFIX_EXPANDED")
    errors = sorted(set(baseline_errors + candidate_errors + binding_errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "baseline_plan_sha256": baseline.get("plan_sha256") if isinstance(baseline, dict) else None,
        "candidate_plan_sha256": candidate.get("plan_sha256")
        if isinstance(candidate, dict)
        else None,
        "errors": errors,
        "non_escalation": {
            "trusted_prefix_expansion": False,
            "mandatory_action_suppression": False,
            "hash_invention": False,
            "source_edit": False,
            "revalidation_skip": False,
            "repair_execution": False,
            "runtime_or_service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["audit_sha256"] = _digest(result)
    return result


def _set(path: tuple[object, ...], value: object) -> Callable[[dict[str, object]], None]:
    def mutate(plan: dict[str, object]) -> None:
        target: object = plan
        for part in path[:-1]:
            target = target[part]  # type: ignore[index]
        target[path[-1]] = value  # type: ignore[index]

    return mutate


def mutation_corpus(
    plan: dict[str, object],
) -> list[tuple[str, Callable[[dict[str, object]], None]]]:
    count = int(plan["trusted_prefix_count"])
    mutations: list[tuple[str, Callable[[dict[str, object]], None]]] = [
        ("schema", _set(("schema",), "phase4mb.evil.v1")),
        ("verdict", _set(("verdict",), "REFUSE")),
        ("source-hash", _set(("source_checkpoints_sha256",), "f" * 64)),
        ("manifest-hash", _set(("manifest_sha256",), "f" * 64)),
        ("trust-prefix", _set(("trusted_prefix_count",), min(len(STATES), count + 1))),
        ("trusted-checkpoint", _set(("trusted_checkpoint_sha256",), "f" * 64)),
        ("boundary", _set(("corruption_boundary_index",), 0)),
        ("boundary-errors", _set(("boundary_errors",), [])),
        ("actions-clear", _set(("actions",), [])),
        ("action-count", _set(("minimality", "action_count"), 0)),
        ("source-edit", _set(("minimality", "source_edit_authorized"), True)),
        ("hash-invention", _set(("minimality", "hash_invention_authorized"), True)),
        ("skip-revalidation", _set(("minimality", "revalidation_skip_authorized"), True)),
        ("planning-only", _set(("safety", "planning_only"), False)),
    ]
    for field in sorted(SAFETY - {"planning_only"}):
        mutations.append((f"capability-{field}", _set(("safety", field), True)))
    action_names = [row.get("action") for row in plan.get("actions", [])]
    for index, name in enumerate(action_names):
        mutations.append((f"drop-{name}", lambda value, i=index: value["actions"].pop(i)))
    if "REACQUIRE_EVIDENCE" in action_names:
        index = action_names.index("REACQUIRE_EVIDENCE")
        mutations.append(("reacquire-state", _set(("actions", index, "from_state"), "CLOSED")))
    if "REPLAY_FROM_CHECKPOINT" in action_names:
        index = action_names.index("REPLAY_FROM_CHECKPOINT")
        mutations.append(("replay-hash", _set(("actions", index, "checkpoint_sha256"), "f" * 64)))
    if "REBUILD_RESUME_TOKEN" in action_names:
        index = action_names.index("REBUILD_RESUME_TOKEN")
        mutations.append(
            ("disable-revalidation", _set(("actions", index, "revalidate_invariants"), False))
        )
    return mutations


def run_mutation_audit(plan: object) -> dict[str, object]:
    baseline_errors = plan_errors(plan)
    records: list[dict[str, object]] = []
    if not baseline_errors and isinstance(plan, dict):
        for mutation_id, mutate in mutation_corpus(plan):
            candidate = copy.deepcopy(plan)
            mutate(candidate)
            candidate["plan_sha256"] = _body_hash(candidate)
            audit = audit_candidate(plan, candidate)
            records.append(
                {
                    "mutation_id": mutation_id,
                    "rejected": audit["verdict"] == "REFUSE",
                    "errors": audit["errors"],
                    "candidate_sha256": candidate["plan_sha256"],
                }
            )
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS"
        if not baseline_errors and records and all(row["rejected"] for row in records)
        else "REFUSE",
        "baseline_errors": baseline_errors,
        "mutation_count": len(records),
        "rejected_count": sum(bool(row["rejected"]) for row in records),
        "records": records,
        "safety": {
            "read_only": True,
            "repair_execution": False,
            "runtime_write": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["audit_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan")
    args = parser.parse_args()
    with open(args.plan, encoding="utf-8") as stream:
        result = run_mutation_audit(json.load(stream))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
