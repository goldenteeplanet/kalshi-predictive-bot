"""Immutable recovery-baseline lineage and fail-closed rollback proposals."""

from __future__ import annotations

import hashlib
import json

SCHEMA = "phase4oo.baseline-lineage.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def append_baseline(
    history: list[dict[str, object]],
    baseline: dict[str, object],
    *,
    promotion_comparison_sha256: str | None,
) -> list[dict[str, object]]:
    version = len(history) + 1
    if baseline.get("version") != version:
        raise ValueError("baseline version must append exactly once")
    if version == 1 and promotion_comparison_sha256 is not None:
        raise ValueError("genesis cannot have promotion evidence")
    if version > 1 and not promotion_comparison_sha256:
        raise ValueError("promotion evidence is required")
    body = {
        "schema": SCHEMA,
        "version": version,
        "baseline_sha256": baseline.get("baseline_sha256"),
        "soak_sha256": baseline.get("soak_sha256"),
        "parent_entry_sha256": history[-1]["entry_sha256"] if history else None,
        "promotion_comparison_sha256": promotion_comparison_sha256,
        "performance_claim": True,
        "safety": _safety(),
    }
    return [*history, {**body, "entry_sha256": _digest(body)}]


def verify_history(
    history: list[dict[str, object]], *, trusted_head_sha256: str, minimum_retained: int
) -> dict[str, object]:
    errors: list[str] = []
    seen_baselines, seen_entries = set(), set()
    for index, entry in enumerate(history, start=1):
        unsigned = {key: value for key, value in entry.items() if key != "entry_sha256"}
        if entry.get("entry_sha256") != _digest(unsigned):
            errors.append("LINEAGE_ENTRY_HASH_MISMATCH")
        if entry.get("entry_sha256") in seen_entries:
            errors.append("LINEAGE_ENTRY_REPLAY")
        seen_entries.add(entry.get("entry_sha256"))
        if entry.get("baseline_sha256") in seen_baselines:
            errors.append("BASELINE_REUSED")
        seen_baselines.add(entry.get("baseline_sha256"))
        if entry.get("version") != index:
            errors.append("VERSION_GAP_OR_ROLLBACK")
        expected_parent = None if index == 1 else history[index - 2].get("entry_sha256")
        if entry.get("parent_entry_sha256") != expected_parent:
            errors.append("LINEAGE_PARENT_MISMATCH")
        promotion = entry.get("promotion_comparison_sha256")
        if (index == 1 and promotion is not None) or (index > 1 and not promotion):
            errors.append("PROMOTION_EVIDENCE_INVALID")
        if entry.get("performance_claim") is not True or entry.get("safety") != _safety():
            errors.append("LINEAGE_CLAIM_OR_SAFETY_INVALID")
    head = history[-1].get("entry_sha256") if history else None
    if head != trusted_head_sha256:
        errors.append("TRUSTED_HEAD_MISMATCH")
    if len(history) < minimum_retained:
        errors.append("RETENTION_REQUIREMENT_NOT_MET")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "versions": [entry.get("version") for entry in history],
        "retained_count": len(history),
        "head_sha256": head,
        "safety": _safety(),
    }
    return {**body, "verification_sha256": _digest(body)}


def propose_rollback(
    history: list[dict[str, object]],
    *,
    trusted_head_sha256: str,
    target_version: int,
    reason: str,
) -> dict[str, object]:
    verification = verify_history(
        history, trusted_head_sha256=trusted_head_sha256, minimum_retained=len(history)
    )
    errors = []
    if verification["verdict"] != "PASS":
        errors.append("LINEAGE_NOT_VERIFIED")
    if target_version < 1 or target_version >= len(history):
        errors.append("ROLLBACK_TARGET_INVALID")
    if not reason.strip():
        errors.append("ROLLBACK_REASON_REQUIRED")
    target = history[target_version - 1] if 1 <= target_version <= len(history) else None
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "mode": "FROZEN_OPERATIONAL_ROLLBACK",
        "trusted_head_sha256": trusted_head_sha256,
        "target_version": target_version,
        "target_baseline_sha256": target.get("baseline_sha256") if target else None,
        "reason": reason,
        "history_retained": True,
        "head_rewritten": False,
        "performance_claim": False,
        "capabilities_allowed": False,
        "safety": _safety(),
    }
    return {**body, "rollback_sha256": _digest(body)}


def detect_forks(histories: list[list[dict[str, object]]]) -> dict[str, object]:
    children: dict[object, set[object]] = {}
    for history in histories:
        for entry in history[1:]:
            children.setdefault(entry.get("parent_entry_sha256"), set()).add(
                entry.get("entry_sha256")
            )
    forks = sorted(str(parent) for parent, values in children.items() if len(values) > 1)
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not forks else "REFUSE",
        "errors": [] if not forks else ["LINEAGE_FORK_DETECTED"],
        "fork_parents": forks,
        "safety": _safety(),
    }
    return {**body, "fork_scan_sha256": _digest(body)}


def _safety() -> dict[str, bool]:
    return {
        "offline_only": True,
        "infrastructure_mutation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
