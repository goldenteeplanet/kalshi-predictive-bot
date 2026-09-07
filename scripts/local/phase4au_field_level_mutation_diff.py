"""Phase 4AU read-only proof that only one intended sandbox field changed."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4au.intended-change-proof.v1"
PRESERVATION_SCHEMA = "phase4au.unrelated-state-preservation-proof.v1"
AT_SCHEMA = "phase4at.sandbox-simulation-receipt.v1"
MARKER_SCHEMA = "phase4al.disposable-simulation-database.v1"


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AU_RECEIPT_UNREADABLE") from exc
    if payload.get("schema") != AT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4AU_RECEIPT_SCHEMA_OR_HASH_INVALID")
    if payload.get("simulation_state") != "SANDBOX_SIMULATION_COMMITTED":
        raise ValueError("PHASE4AU_RECEIPT_NOT_COMMITTED")
    if payload.get("affected_row_count") != 1 or payload.get("execution_authorized") is not False:
        raise ValueError("PHASE4AU_RECEIPT_SCOPE_INVALID")
    return payload


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _snapshot(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        marker = connection.execute(
            "SELECT marker_schema, disposable FROM phase4al_disposable_marker WHERE id=1"
        ).fetchone()
        if marker is None or marker["marker_schema"] != MARKER_SCHEMA or marker["disposable"] != 1:
            raise ValueError("PHASE4AU_DISPOSABLE_MARKER_INVALID")
        objects = [
            dict(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        ]
        tables: dict[str, Any] = {}
        for name in sorted(item["name"] for item in objects if item["type"] == "table"):
            columns = [
                dict(row) for row in connection.execute(f"PRAGMA table_xinfo({_quote(name)})")
            ]
            column_names = [str(row["name"]) for row in columns]
            order = ",".join(_quote(column) for column in column_names)
            rows = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {_quote(name)} ORDER BY {order}"  # noqa: S608
                )
            ]
            indexes = [
                dict(row) for row in connection.execute(f"PRAGMA index_list({_quote(name)})")
            ]
            foreign_keys = [
                dict(row) for row in connection.execute(f"PRAGMA foreign_key_list({_quote(name)})")
            ]
            tables[name] = {
                "columns": columns,
                "indexes": indexes,
                "foreign_keys": foreign_keys,
                "rows": rows,
            }
        metadata = {
            name: connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("application_id", "user_version", "encoding")
        }
    except sqlite3.Error as exc:
        raise ValueError("PHASE4AU_DATABASE_INVALID_OR_UNREADABLE") from exc
    finally:
        connection.close()
    return {
        "resolved_path_hash": canonical_hash(str(resolved)),
        "objects": objects,
        "tables": tables,
        "metadata": metadata,
    }


def build(
    before_database: Path,
    after_database: Path,
    receipt_path: Path,
    *,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AU_EVALUATION_TIMEZONE_MISSING")
    before_resolved = before_database.resolve(strict=True)
    after_resolved = after_database.resolve(strict=True)
    before_stat, after_stat = before_resolved.stat(), after_resolved.stat()
    if before_resolved == after_resolved or (before_stat.st_dev, before_stat.st_ino) == (
        after_stat.st_dev,
        after_stat.st_ino,
    ):
        raise ValueError("PHASE4AU_SNAPSHOTS_NOT_DISTINCT")
    receipt = _load_receipt(receipt_path)
    before, after = _snapshot(before_database), _snapshot(after_database)
    if before["objects"] != after["objects"]:
        raise ValueError("PHASE4AU_SCHEMA_OBJECTS_CHANGED")
    if before["metadata"] != after["metadata"]:
        raise ValueError("PHASE4AU_APPLICATION_METADATA_CHANGED")
    if set(before["tables"]) != set(after["tables"]):
        raise ValueError("PHASE4AU_TABLE_SET_CHANGED")

    ticker = receipt.get("ticker")
    intended_changes: list[dict[str, Any]] = []
    preservation_hashes: dict[str, str] = {}
    for table in sorted(before["tables"]):
        old_table, new_table = before["tables"][table], after["tables"][table]
        if old_table["columns"] != new_table["columns"]:
            raise ValueError("PHASE4AU_COLUMN_METADATA_CHANGED")
        if old_table["indexes"] != new_table["indexes"]:
            raise ValueError("PHASE4AU_INDEX_METADATA_CHANGED")
        if old_table["foreign_keys"] != new_table["foreign_keys"]:
            raise ValueError("PHASE4AU_CONSTRAINT_METADATA_CHANGED")
        old_rows, new_rows = old_table["rows"], new_table["rows"]
        if len(old_rows) != len(new_rows):
            raise ValueError("PHASE4AU_ROW_COUNT_CHANGED")
        for old_row, new_row in zip(old_rows, new_rows, strict=True):
            if old_row == new_row:
                continue
            changed_fields = sorted(
                field for field in old_row if old_row.get(field) != new_row.get(field)
            )
            if (
                table != "settlements"
                or old_row.get("ticker") != ticker
                or new_row.get("ticker") != ticker
                or changed_fields != ["settled_at"]
            ):
                raise ValueError("PHASE4AU_UNINTENDED_STATE_CHANGE")
            intended_changes.append(
                {
                    "table": table,
                    "ticker": ticker,
                    "field": "settled_at",
                    "before": old_row.get("settled_at"),
                    "after": new_row.get("settled_at"),
                    "before_row_hash": canonical_hash(old_row),
                    "after_row_hash": canonical_hash(new_row),
                }
            )
        preservation_hashes[table] = canonical_hash(
            {
                "before": [
                    row
                    for row in old_rows
                    if not (table == "settlements" and row.get("ticker") == ticker)
                ],
                "after": [
                    row
                    for row in new_rows
                    if not (table == "settlements" and row.get("ticker") == ticker)
                ],
            }
        )
    if len(intended_changes) != 1:
        raise ValueError("PHASE4AU_EXACTLY_ONE_INTENDED_CHANGE_REQUIRED")
    change = intended_changes[0]
    if change["before"] not in (None, "") or change["before_row_hash"] != receipt.get(
        "before_row_hash"
    ):
        raise ValueError("PHASE4AU_BEFORE_RECEIPT_MISMATCH")
    if change["after_row_hash"] != receipt.get("after_row_hash"):
        raise ValueError("PHASE4AU_AFTER_RECEIPT_MISMATCH")

    evaluated_at = now.astimezone(UTC).isoformat()
    proof: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AU",
        "evaluated_at": evaluated_at,
        "phase4at_receipt_hash": receipt["artifact_hash"],
        "change": change,
        "exactly_one_field_changed": True,
        "database_access_mode": "READ_ONLY",
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    preservation: dict[str, Any] = {
        "schema": PRESERVATION_SCHEMA,
        "phase": "4AU",
        "evaluated_at": evaluated_at,
        "intended_change_proof_hash": proof["artifact_hash"],
        "schema_objects_preserved": True,
        "columns_indexes_constraints_preserved": True,
        "application_metadata_preserved": True,
        "row_counts_preserved": True,
        "unrelated_rows_preserved": True,
        "table_preservation_hashes": dict(sorted(preservation_hashes.items())),
        "production_database_mutated": False,
        "research_database_mutated": False,
        "database_mutation_performed": False,
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    preservation["artifact_hash"] = _hash(preservation)
    return proof, preservation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-disposable-db", type=Path, required=True)
    parser.add_argument("--after-disposable-db", type=Path, required=True)
    parser.add_argument("--phase4at-receipt", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--change-proof-output", type=Path, required=True)
    parser.add_argument("--preservation-proof-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    proof, preservation = build(
        args.before_disposable_db, args.after_disposable_db, args.phase4at_receipt, now=now
    )
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.change_proof_output, args.preservation_proof_output, proof, preservation)
    print(json.dumps(proof, sort_keys=True))


if __name__ == "__main__":
    main()
