"""PROV-6 guarded cloud migration preflight report generator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now


EXPECTED_PARENT_REVISION = "20260624_0011"
DATABASE = "/var/lib/kalshi-bot/kalshi_phase1.db"
BACKUP_MOUNT = "/mnt/kalshi-backup"


def write_prov6_preflight(*, evidence: dict[str, Any], prov5_report: Path,
                          output_dir: Path) -> Path:
    prov5 = json.loads(prov5_report.read_text(encoding="utf-8"))
    database_bytes = int(evidence["schema"]["database_bytes"])
    required_backup_bytes = int(database_bytes * 1.25)
    available = int(evidence["disk"]["available_bytes"])
    indexes = evidence["schema"].get("conflicting_provenance_indexes", [])
    provenance_conflicts = [row for row in indexes if "provenance" in str(row.get("name", ""))]
    gates = {
        "required_runtime_tables_present": all(
            evidence["schema"]["required_tables_present"].values()
        ),
        "provenance_schema_absent": not evidence["schema"]["provenance_table_present"],
        "no_provenance_name_conflicts": not provenance_conflicts,
        "alembic_parent_revision_matches": (
            evidence["schema"].get("alembic_revision") == EXPECTED_PARENT_REVISION
        ),
        "root_disk_can_hold_verified_backup": available >= required_backup_bytes,
        "external_backup_mount_ready": bool(evidence["disk"].get("external_backup_mount_ready")),
        "writer_monitor_clear": evidence["writer"]["writer_count"] == 0,
        "no_open_database_holders": evidence["writer"]["holder_count"] == 0,
        "dual_write_disabled": evidence["runtime"]["dual_write_enabled"] is False,
        "execution_disabled": evidence["runtime"]["execution_enabled"] is False,
        "offline_migration_certified": bool(prov5["summary"]["migration_passed"]),
        "offline_rollback_certified": bool(prov5["summary"]["rollback_passed"]),
    }
    ready = all(gates.values())
    commands = _commands(required_backup_bytes)
    report = {
        "phase": "PROV-6", "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_CLOUD_MIGRATION_PREFLIGHT",
        "cloud_database_writes": 0, "migration_applied": False,
        "backup_created": False, "dual_write_enabled": False,
        "execution_enabled": False, "evidence": evidence,
        "offline_performance": {
            "volume_rows": prov5["summary"]["volume_rows_certified"],
            "migration_seconds": prov5["migration"]["seconds"],
            "backfill_rows_per_second": prov5["backfill"]["first_run"]["rows_per_second"],
            "note": "Empty-table migration time is independent of the 20 GB legacy row count; "
                    "cloud filesystem sync latency remains unmeasured.",
        },
        "capacity": {"database_bytes": database_bytes,
                     "minimum_backup_destination_bytes": required_backup_bytes,
                     "root_available_bytes": available,
                     "root_shortfall_bytes": max(0, required_backup_bytes - available)},
        "gates": gates,
        "commands": commands,
        "summary": {
            "preflight_passed": ready,
            "approval_requested": False,
            "blocked_gates": sorted(key for key, passed in gates.items() if not passed),
            "next_action": (
                "STOP_FOR_EXPLICIT_APPROVAL" if ready else
                "REMEDIATE_PREFLIGHT_BLOCKERS_AND_RERUN"
            ),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov6_guarded_cloud_migration_preflight.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _commands(required_bytes: int) -> dict[str, list[str]]:
    return {
        "preflight": [
            "cd /opt/kalshi-predictive-bot",
            "set -a; . /etc/kalshi-bot/kalshi-bot.env; set +a",
            ".venv/bin/kalshi-bot db-writer-monitor --json",
            ".venv/bin/kalshi-bot db-locks",
            f"test \"$(sqlite3 -readonly {DATABASE} 'SELECT version_num FROM alembic_version LIMIT 1;')\" = '{EXPECTED_PARENT_REVISION}'",
            f"mountpoint -q {BACKUP_MOUNT}",
            f"test \"$(df -B1 --output=avail {BACKUP_MOUNT} | tail -1)\" -ge {required_bytes}",
            "test \"${RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED:-false}\" = false",
            "test \"${EXECUTION_ENABLED:-false}\" = false",
        ],
        "backup_after_all_gates_pass": [
            f"DB={DATABASE}", f"BACKUP_ROOT={BACKUP_MOUNT}",
            "STAMP=$(date -u +%Y%m%dT%H%M%SZ)",
            "TMP=$BACKUP_ROOT/kalshi_phase1_pre_prov6_${STAMP}.db.tmp",
            "FINAL=$BACKUP_ROOT/kalshi_phase1_pre_prov6_${STAMP}.db",
            "flock -n /var/lib/kalshi-bot/prov6-migration.lock sqlite3 \"$DB\" \".timeout 60000\" \".backup '$TMP'\"",
            "test \"$(sqlite3 -readonly \"$TMP\" 'PRAGMA integrity_check;')\" = ok",
            "mv \"$TMP\" \"$FINAL\"",
            "sha256sum \"$FINAL\" | tee \"$FINAL.sha256\"",
            "stat -c '%n %s bytes' \"$FINAL\"",
        ],
        "migration_after_separate_approval": [
            "# DO NOT RUN WITHOUT EXPLICIT APPROVAL",
            "RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=false .venv/bin/alembic upgrade 20260716_0012",
            f"sqlite3 -readonly {DATABASE} \"SELECT name FROM sqlite_master WHERE name='runtime_provenance_events';\"",
        ],
        "rollback": [
            "export RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=false",
            "# Stop any approved provenance backfill before downgrade.",
            f".venv/bin/alembic downgrade {EXPECTED_PARENT_REVISION}",
            f"sqlite3 -readonly {DATABASE} \"SELECT name FROM sqlite_master WHERE name='runtime_provenance_events';\"",
            "# Restore the verified backup only if legacy-table verification fails.",
        ],
    }
