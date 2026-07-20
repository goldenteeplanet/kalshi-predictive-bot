"""PROV-6A no-write backup-volume and Alembic lineage reconciliation preview."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now


EXPECTED_COLUMNS = {
    "id", "ticker", "leg_index", "parsed_at", "side", "category", "market_type",
    "entity_name", "operator", "threshold_value", "unit", "confidence", "raw_text",
    "reason", "raw_json",
}
EXPECTED_REVISION = "20260624_0011"


def write_prov6a_preview(*, evidence: dict[str, Any], output_dir: Path) -> Path:
    lineage = evidence["lineage"]
    columns = {row["name"] for row in lineage.get("market_legs_columns", [])}
    indexes = {row["name"]: row.get("columns", [])
               for row in lineage.get("market_legs_indexes", [])}
    schema_equivalent = (
        lineage.get("market_legs_present") is True
        and columns == EXPECTED_COLUMNS
        and indexes.get("ix_market_legs_category_ticker") == ["category", "ticker"]
        and any(row.get("unique") == 1 and row.get("columns") == ["ticker", "leg_index"]
                for row in lineage.get("market_legs_indexes", []))
    )
    revision_matches = lineage.get("alembic_revision") == EXPECTED_REVISION
    storage = evidence["storage"]
    gates = {
        "revision_0011_schema_equivalent": schema_equivalent,
        "revision_file_present_and_hash_matched": bool(
            lineage.get("revision_file_present") and lineage.get("revision_file_hash_matched")
        ),
        "alembic_marker_current": revision_matches,
        "external_backup_device_attached": bool(storage.get("external_device_attached")),
        "backup_mount_ready": bool(storage.get("backup_mount_ready")),
        "backup_capacity_sufficient": int(storage.get("backup_available_bytes") or 0) >= int(
            storage["minimum_required_bytes"]
        ),
        "no_open_database_holders": int(evidence["writer"].get("holder_count") or 0) == 0,
        "no_active_writer": int(evidence["writer"].get("writer_count") or 0) == 0,
    }
    report = {
        "phase": "PROV-6A", "generated_at": utc_now().isoformat(),
        "mode": "NO_WRITE_BACKUP_VOLUME_ALEMBIC_LINEAGE_RECONCILIATION_PREVIEW",
        "cloud_database_writes": 0, "volume_attached_or_mounted": False,
        "alembic_stamped_or_upgraded": False, "readers_stopped": False,
        "migration_applied": False, "execution_enabled": False,
        "evidence": evidence,
        "lineage_assessment": {
            "schema_equivalent_to_0011": schema_equivalent,
            "marker_revision": lineage.get("alembic_revision"),
            "expected_revision": EXPECTED_REVISION,
            "classification": (
                "SCHEMA_PRESENT_MARKER_STALE" if schema_equivalent and not revision_matches
                else "LINEAGE_REQUIRES_FURTHER_REVIEW"
            ),
            "extra_safe_indexes": sorted(set(indexes) - {
                "ix_market_legs_category_ticker", "sqlite_autoindex_market_legs_1"
            }),
        },
        "storage_assessment": {
            "attached_block_devices": storage.get("block_devices", []),
            "external_backup_device_attached": storage.get("external_device_attached"),
            "backup_mount": "/mnt/kalshi-backup",
            "recommended_new_volume_bytes": 50 * 1024**3,
            "minimum_required_bytes": storage["minimum_required_bytes"],
            "local_archive_alternative": (
                "D:\\Kalshi Bot Archive has ample capacity, but it is not mounted on the cloud; "
                "prefer an attached cloud block volume for SQLite .backup."
            ),
        },
        "reconciliation_plan": {
            "apply_authorized": False,
            "preferred_marker_repair": (
                "After backup and reader clearance, run Alembic upgrade to 20260624_0011. "
                "The revision uses create(checkfirst=True), so the matching table is retained "
                "and Alembic advances the marker."
            ),
            "stamp_only_alternative": (
                "alembic stamp 20260624_0011 is technically possible but not preferred because "
                "upgrade records the normal lineage path."
            ),
            "commands_after_separate_approval": [
                "# Attach a new >=50 GiB cloud block volume in the provider control plane first.",
                "lsblk -f  # identify the new unformatted device; do not assume /dev/sda",
                "# Format and mount only the verified new empty device, then persist its UUID in /etc/fstab.",
                "mountpoint -q /mnt/kalshi-backup",
                "test \"$(df -B1 --output=avail /mnt/kalshi-backup | tail -1)\" -ge 25411691520",
                "# Create and integrity-check the PROV-6 binary backup using the commands in the PROV-6 report.",
                "cd /opt/kalshi-predictive-bot",
                "set -a; . /etc/kalshi-bot/kalshi-bot.env; set +a",
                ".venv/bin/kalshi-bot db-writer-monitor --json",
                ".venv/bin/kalshi-bot db-locks",
                "RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=false .venv/bin/alembic upgrade 20260624_0011",
                "test \"$(sqlite3 -readonly /var/lib/kalshi-bot/kalshi_phase1.db 'SELECT version_num FROM alembic_version;')\" = '20260624_0011'",
            ],
        },
        "gates": gates,
        "summary": {
            "schema_0011_present": schema_equivalent,
            "marker_stale": schema_equivalent and not revision_matches,
            "preflight_ready_for_approval": all(gates.values()),
            "blocked_gates": sorted(key for key, passed in gates.items() if not passed),
            "next_action": "ATTACH_BACKUP_VOLUME_CLEAR_READERS_AND_RERUN_PREFLIGHT",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov6a_backup_volume_alembic_lineage_preview.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path
