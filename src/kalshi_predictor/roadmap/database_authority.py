from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DatabaseParityEvidence:
    sqlite_counts: dict[str, int]
    postgres_counts: dict[str, int]
    sqlite_schema_revision: str
    postgres_schema_revision: str
    timestamp_mismatches: int = 0
    foreign_key_violations: int = 0
    idempotency_mismatches: int = 0
    pnl_mismatches: int = 0
    settlement_mismatches: int = 0
    report_mismatches: int = 0
    backup_restore_passed: bool = False
    rollback_rehearsal_passed: bool = False
    concurrency_rehearsal_passed: bool = False


def certify_postgres_authority(evidence: DatabaseParityEvidence) -> dict[str, Any]:
    all_tables = sorted(set(evidence.sqlite_counts) | set(evidence.postgres_counts))
    count_mismatches = {
        table: {
            "sqlite": int(evidence.sqlite_counts.get(table, 0)),
            "postgres": int(evidence.postgres_counts.get(table, 0)),
        }
        for table in all_tables
        if evidence.sqlite_counts.get(table, 0) != evidence.postgres_counts.get(table, 0)
    }
    checks = {
        "schema_revision_matches": (
            evidence.sqlite_schema_revision == evidence.postgres_schema_revision
        ),
        "table_counts_match": not count_mismatches,
        "timestamps_match": evidence.timestamp_mismatches == 0,
        "foreign_keys_valid": evidence.foreign_key_violations == 0,
        "idempotency_matches": evidence.idempotency_mismatches == 0,
        "paper_pnl_matches": evidence.pnl_mismatches == 0,
        "settlements_match": evidence.settlement_mismatches == 0,
        "reports_match": evidence.report_mismatches == 0,
        "backup_restore": evidence.backup_restore_passed,
        "rollback_rehearsal": evidence.rollback_rehearsal_passed,
        "concurrency_rehearsal": evidence.concurrency_rehearsal_passed,
    }
    return {
        "schema_version": "postgres-authority-certification-v1",
        "passed": all(checks.values()),
        "checks": checks,
        "count_mismatches": count_mismatches,
        "evidence": asdict(evidence),
        "postgres_write_authority_enabled": False,
        "live_execution_enabled": False,
    }
