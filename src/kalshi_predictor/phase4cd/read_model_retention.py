from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from kalshi_predictor.phase4cd.read_model_chain import validate_chain

RETENTION_SCHEMA_VERSION = "phase4fr-read-model-retention-plan-v1"


class RetentionPolicyError(ValueError):
    """Stable fail-closed retention planning error."""


def plan_retention(
    nodes: Any,
    *,
    keep_last: int,
    max_remove: int,
    now: datetime,
    max_head_age_seconds: float,
    max_nodes: int,
) -> dict[str, Any]:
    if not isinstance(keep_last, int) or isinstance(keep_last, bool) or keep_last <= 0:
        raise RetentionPolicyError("KEEP_LAST_INVALID")
    if not isinstance(max_remove, int) or isinstance(max_remove, bool) or max_remove < 0:
        raise RetentionPolicyError("MAX_REMOVE_INVALID")
    try:
        chain = validate_chain(
            nodes,
            now=now,
            max_head_age_seconds=max_head_age_seconds,
            max_nodes=max_nodes,
        )
    except ValueError as exc:
        raise RetentionPolicyError(f"SOURCE_CHAIN_INVALID:{exc}") from exc
    remove_count = max(0, chain.node_count - keep_last)
    if remove_count > max_remove:
        raise RetentionPolicyError("REMOVE_BOUND_EXCEEDED")
    removed = nodes[:remove_count]
    retained = nodes[remove_count:]
    anchor = None
    if removed:
        removed_head = removed[-1]
        retained_first = retained[0]
        if retained_first["previous_node_hash"] != removed_head["node_hash"]:
            raise RetentionPolicyError("RETENTION_BOUNDARY_BROKEN")
        anchor = {
            "original_genesis_hash": nodes[0]["node_hash"],
            "removed_head_hash": removed_head["node_hash"],
            "removed_head_sequence": removed_head["sequence"],
            "retained_first_hash": retained_first["node_hash"],
            "retained_first_previous_hash": retained_first["previous_node_hash"],
            "retained_first_sequence": retained_first["sequence"],
        }
    payload: dict[str, Any] = {
        "schema_version": RETENTION_SCHEMA_VERSION,
        "policy": {"keep_last": keep_last, "max_remove": max_remove},
        "source_chain_head_hash": chain.head_hash,
        "source_chain_node_count": chain.node_count,
        "remove_node_hashes": [node["node_hash"] for node in removed],
        "retain_node_hashes": [node["node_hash"] for node in retained],
        "retention_anchor": anchor,
        "operation": "PLAN_ONLY_NO_DELETE",
    }
    payload["plan_hash"] = _hash(payload)
    return payload


def validate_retention_plan(
    plan: Any,
    nodes: Any,
    *,
    now: datetime,
    max_head_age_seconds: float,
    max_nodes: int,
) -> None:
    required = {
        "schema_version",
        "policy",
        "source_chain_head_hash",
        "source_chain_node_count",
        "remove_node_hashes",
        "retain_node_hashes",
        "retention_anchor",
        "operation",
        "plan_hash",
    }
    if not isinstance(plan, dict) or set(plan) != required:
        raise RetentionPolicyError("PLAN_FIELDS_INVALID")
    if plan["schema_version"] != RETENTION_SCHEMA_VERSION:
        raise RetentionPolicyError("PLAN_SCHEMA_UNSUPPORTED")
    if plan["operation"] != "PLAN_ONLY_NO_DELETE":
        raise RetentionPolicyError("PLAN_OPERATION_INVALID")
    if not isinstance(plan["policy"], dict) or set(plan["policy"]) != {
        "keep_last",
        "max_remove",
    }:
        raise RetentionPolicyError("PLAN_POLICY_INVALID")
    unhashed = {key: value for key, value in plan.items() if key != "plan_hash"}
    if plan["plan_hash"] != _hash(unhashed):
        raise RetentionPolicyError("PLAN_HASH_MISMATCH")
    expected = plan_retention(
        nodes,
        keep_last=plan["policy"]["keep_last"],
        max_remove=plan["policy"]["max_remove"],
        now=now,
        max_head_age_seconds=max_head_age_seconds,
        max_nodes=max_nodes,
    )
    if plan != expected:
        raise RetentionPolicyError("PLAN_SOURCE_MISMATCH")


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
