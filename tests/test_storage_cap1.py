from __future__ import annotations

from kalshi_predictor.storage_capacity import GIB, StoragePolicy, build_capacity_plan


def backup(path: str, *, active: bool, archived: bool = True):
    return {
        "path": path,
        "bytes": 21 * GIB,
        "created_at": "2026-07-18T00:00:00Z",
        "integrity": "ok",
        "sha256": "a" * 64,
        "active_rollback": active,
        "archive_verified": archived,
        "local_archive_sha256_match": archived,
    }


def test_verified_archive_restores_required_reserve() -> None:
    plan = build_capacity_plan(
        volume_total_bytes=49 * GIB,
        volume_free_bytes=6 * GIB,
        database_bytes=21 * GIB,
        backups=[backup("active.db", active=True), backup("cold.db", active=False)],
    )
    assert plan["status"] == "PASSED_LOCAL_PREVIEW"
    assert plan["capacity"]["safe_after_archive"] is True
    assert plan["files_deleted"] == 0
    assert plan["next_phase_requires_approval"] is True


def test_unverified_archive_hash_blocks_plan() -> None:
    cold = backup("cold.db", active=False)
    cold["local_archive_sha256_match"] = False
    plan = build_capacity_plan(
        volume_total_bytes=49 * GIB,
        volume_free_bytes=5 * GIB,
        database_bytes=21 * GIB,
        backups=[backup("active.db", active=True), cold],
    )
    assert plan["status"] == "BLOCKED"
    assert "ARCHIVE_HASH_NOT_VERIFIED" in plan["failures"]


def test_active_rollback_backup_is_never_archive_candidate() -> None:
    active = backup("active.db", active=True)
    plan = build_capacity_plan(
        volume_total_bytes=49 * GIB,
        volume_free_bytes=30 * GIB,
        database_bytes=21 * GIB,
        backups=[active],
    )
    assert plan["archive_candidates"] == []
    assert plan["protected_backups"] == [active]


def test_insufficient_reclaimable_capacity_blocks() -> None:
    plan = build_capacity_plan(
        volume_total_bytes=49 * GIB,
        volume_free_bytes=5 * GIB,
        database_bytes=30 * GIB,
        backups=[backup("active.db", active=True)],
    )
    assert plan["status"] == "BLOCKED"
    assert "INSUFFICIENT_RECLAIMABLE_CAPACITY" in plan["failures"]


def test_policy_uses_larger_of_absolute_and_growth_reserves() -> None:
    policy = StoragePolicy(
        minimum_absolute_free_bytes=24 * GIB,
        backup_growth_factor=1.15,
        verification_headroom_bytes=2 * GIB,
    )
    small = build_capacity_plan(
        volume_total_bytes=100 * GIB,
        volume_free_bytes=50 * GIB,
        database_bytes=10 * GIB,
        backups=[backup("active.db", active=True)],
        policy=policy,
    )
    large = build_capacity_plan(
        volume_total_bytes=100 * GIB,
        volume_free_bytes=50 * GIB,
        database_bytes=30 * GIB,
        backups=[backup("active.db", active=True)],
        policy=policy,
    )
    assert small["policy"]["required_free_reserve_bytes"] == 24 * GIB
    assert large["policy"]["required_free_reserve_bytes"] > 34 * GIB
