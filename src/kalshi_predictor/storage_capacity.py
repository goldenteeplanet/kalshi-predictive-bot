from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GIB = 1024**3


@dataclass(frozen=True)
class StoragePolicy:
    minimum_absolute_free_bytes: int = 24 * GIB
    backup_growth_factor: float = 1.15
    verification_headroom_bytes: int = 2 * GIB
    minimum_verified_rollback_backups: int = 1


def required_free_reserve(database_bytes: int, policy: StoragePolicy | None = None) -> int:
    policy = policy or StoragePolicy()
    next_backup = math.ceil(database_bytes * policy.backup_growth_factor)
    return max(
        policy.minimum_absolute_free_bytes,
        next_backup + policy.verification_headroom_bytes,
    )


def build_capacity_plan(
    *,
    volume_total_bytes: int,
    volume_free_bytes: int,
    database_bytes: int,
    backups: list[dict[str, Any]],
    policy: StoragePolicy | None = None,
) -> dict[str, Any]:
    policy = policy or StoragePolicy()
    reserve = required_free_reserve(database_bytes, policy)
    deficit = max(0, reserve - volume_free_bytes)
    verified = [item for item in backups if item.get("integrity") == "ok" and item.get("sha256")]
    protected = [item for item in verified if item.get("active_rollback") is True]
    archive_candidates = sorted(
        (
            item
            for item in verified
            if item.get("active_rollback") is not True and item.get("archive_verified") is True
        ),
        key=lambda item: (str(item.get("created_at", "")), str(item.get("path", ""))),
    )
    reclaimable = sum(int(item.get("bytes", 0)) for item in archive_candidates)
    post_archive_free = volume_free_bytes + reclaimable
    enough_after_archive = post_archive_free >= reserve
    failures: list[str] = []
    if len(protected) < policy.minimum_verified_rollback_backups:
        failures.append("ACTIVE_ROLLBACK_BACKUP_MISSING")
    if not enough_after_archive:
        failures.append("INSUFFICIENT_RECLAIMABLE_CAPACITY")
    if any(not item.get("local_archive_sha256_match") for item in archive_candidates):
        failures.append("ARCHIVE_HASH_NOT_VERIFIED")
    plan: dict[str, Any] = {
        "phase": "STORAGE-CAP-1",
        "status": "PASSED_LOCAL_PREVIEW" if not failures else "BLOCKED",
        "mode": "LOCAL_READ_ONLY_CAPACITY_MODEL",
        "cloud_changes": 0,
        "files_moved": 0,
        "files_deleted": 0,
        "database_writes": 0,
        "execution_enabled": False,
        "volume": {
            "total_bytes": volume_total_bytes,
            "free_bytes": volume_free_bytes,
            "free_percent": round(volume_free_bytes / volume_total_bytes * 100, 3),
        },
        "policy": {
            "minimum_absolute_free_bytes": policy.minimum_absolute_free_bytes,
            "backup_growth_factor": policy.backup_growth_factor,
            "verification_headroom_bytes": policy.verification_headroom_bytes,
            "required_free_reserve_bytes": reserve,
        },
        "capacity": {
            "current_deficit_bytes": deficit,
            "reclaimable_verified_bytes": reclaimable,
            "projected_free_after_archive_bytes": post_archive_free,
            "projected_reserve_margin_bytes": post_archive_free - reserve,
            "safe_after_archive": enough_after_archive,
        },
        "protected_backups": protected,
        "archive_candidates": archive_candidates,
        "failures": sorted(set(failures)),
        "activation_rules": [
            "Archive candidate must already exist on user-owned storage.",
            "Local archive SHA-256 must match cloud source SHA-256.",
            "Delete only the exact verified cloud source after explicit approval.",
            "Preserve at least one current verified rollback backup on the cloud volume.",
            "Re-run df and inventory after deletion; never infer reclaimed capacity.",
        ],
        "next_phase": "STORAGE-CAP-2 — Verified Cold-Backup Archival and Capacity Restoration",
        "next_phase_requires_approval": True,
    }
    plan["report_sha256"] = hashlib.sha256(
        (json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    return plan


def write_plan(path: Path, plan: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
