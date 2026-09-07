"""Phase 4BA standalone read-only artifact-chain and production-state verifier."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

IDENTITY_SCHEMA = "phase4ba.production-readonly-identity.v1"
SCHEMA = "phase4ba.independent-verification-report.v1"
HUMAN_SCHEMA = "phase4ba.human-verification-report.v1"
PRECEDENCE = (
    "CHAIN_INVALID",
    "PRODUCTION_IDENTITY_DRIFT",
    "CONCURRENT_AUTHORITATIVE_WRITER",
    "PRODUCTION_STATE_DRIFT",
)


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _metadata(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "resolved_path": str(resolved),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def inspect_state(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()[0]
        if query_only != 1:
            raise ValueError("PHASE4BA_QUERY_ONLY_NOT_ENFORCED")
        schema = [
            list(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
        ]
        table_names = [
            row[1] for row in schema if row[0] == "table" and not row[1].startswith("sqlite_")
        ]
        counts: dict[str, int] = {}
        for table in table_names:
            quoted = '"' + table.replace('"', '""') + '"'
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {quoted}"  # noqa: S608
            ).fetchone()[0]
    finally:
        connection.close()
    return {
        "schema_hash": canonical_hash(schema),
        "table_counts": dict(sorted(counts.items())),
        "table_counts_hash": canonical_hash(dict(sorted(counts.items()))),
        "query_only_enforced": True,
    }


def _load_identity(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4BA_IDENTITY_UNREADABLE") from exc
    if payload.get("schema") != IDENTITY_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BA_IDENTITY_SCHEMA_OR_HASH_INVALID")
    return payload


def _reconstruct(directory: Path) -> dict[str, Any]:
    path = Path(__file__).with_name("phase4az_long_chain_history.py")
    spec = importlib.util.spec_from_file_location("phase4az_for_independent_verifier", path)
    if spec is None or spec.loader is None:
        raise ValueError("PHASE4BA_CHAIN_VERIFIER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.reconstruct(directory)


def build(
    history_directory: Path,
    production_identity_path: Path,
    *,
    now: datetime,
) -> tuple[dict[str, Any], str]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BA_EVALUATION_TIMEZONE_MISSING")
    identity = _load_identity(production_identity_path)
    reasons: set[str] = set()
    chain_bundle: dict[str, Any] | None = None
    try:
        chain_bundle = _reconstruct(history_directory)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        reasons.add("CHAIN_INVALID")
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    if "CHAIN_INVALID" not in reasons:
        try:
            database = Path(identity["resolved_path"])
            before = _metadata(database)
            expected_metadata = identity.get("metadata")
            if before != expected_metadata:
                reasons.add("PRODUCTION_IDENTITY_DRIFT")
            else:
                state = inspect_state(database)
                after = _metadata(database)
                if after != before:
                    reasons.add("CONCURRENT_AUTHORITATIVE_WRITER")
                elif state["schema_hash"] != identity.get("schema_hash") or state[
                    "table_counts_hash"
                ] != identity.get("table_counts_hash"):
                    reasons.add("PRODUCTION_STATE_DRIFT")
        except (OSError, KeyError, sqlite3.Error, ValueError):
            reasons.add("PRODUCTION_IDENTITY_DRIFT")
    ordered = [reason for reason in PRECEDENCE if reason in reasons]
    evaluated_at = now.astimezone(UTC).isoformat()
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BA",
        "evaluated_at": evaluated_at,
        "identity_artifact_hash": identity["artifact_hash"],
        "chain_bundle_hash": chain_bundle.get("artifact_hash") if chain_bundle else None,
        "production_metadata_before": before,
        "production_metadata_after": after,
        "production_state": state,
        "first_failure": ordered[0] if ordered else None,
        "reason_codes": ordered,
        "verification_passed": not ordered,
        "database_access_mode": "READ_ONLY",
        "production_database_mutated": False,
        "research_database_mutated": False,
        "database_mutation_performed": False,
        "production_lock_acquired": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    status = "PASS" if report["verification_passed"] else "FAIL"
    detail = "all checks passed" if not ordered else f"first failure: {ordered[0]}"
    human = (
        f"Phase 4BA independent verification: {status}\n"
        f"{detail}\nReport hash: {report['artifact_hash']}\n"
    )
    return report, human


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--production-identity", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--machine-report-output", type=Path, required=True)
    parser.add_argument("--human-report-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    report, human = build(args.history_dir, args.production_identity, now=now)
    _atomic_text(
        args.machine_report_output,
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
    )
    _atomic_text(args.human_report_output, human)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
