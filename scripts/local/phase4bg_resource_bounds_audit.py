"""Phase 4BG synthetic-only scale and resource-boundedness audit."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4bg.scale-resource-audit.v1"
PROOF_SCHEMA = "phase4bg.resource-bounds-proof.v1"
MAX_ROWS = 10_000
MAX_HISTORY = 10_000
MAX_ARTIFACT_BYTES = 2_000_000
MAX_ESTIMATED_MEMORY_BYTES = 64_000_000


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _limits(rows: int, history: int, artifact_bytes: int) -> None:
    for value, maximum, label in (
        (rows, MAX_ROWS, "ROWS"),
        (history, MAX_HISTORY, "HISTORY"),
        (artifact_bytes, MAX_ARTIFACT_BYTES, "ARTIFACT_BYTES"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
            raise ValueError(f"PHASE4BG_{label}_LIMIT_EXCEEDED")
    estimated = rows * 512 + history * 256 + artifact_bytes * 3
    if estimated > MAX_ESTIMATED_MEMORY_BYTES:
        raise ValueError("PHASE4BG_ESTIMATED_MEMORY_LIMIT_EXCEEDED")


def build(
    *, rows: int, history: int, artifact_bytes: int, now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BG_EVALUATION_TIMEZONE_MISSING")
    _limits(rows, history, artifact_bytes)
    tracemalloc.start()
    started = time.perf_counter_ns()
    synthetic_rows = [
        {"ticker": f"KXSYN-{index:08d}", "value": (index * 17) % 997} for index in range(rows)
    ]
    ordered = sorted(synthetic_rows, key=lambda row: (row["ticker"], row["value"]))
    ordering_hash = canonical_hash(ordered)
    deterministic_ordering = ordering_hash == canonical_hash(
        sorted(reversed(synthetic_rows), key=lambda row: (row["ticker"], row["value"]))
    )
    head = "0" * 64
    for index in range(history):
        head = canonical_hash({"sequence": index + 1, "previous": head})
    artifact = ("x" * artifact_bytes).encode()
    artifact_hash = canonical_hash(artifact.hex())
    validation_elapsed_ns = time.perf_counter_ns() - started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    publication_started = time.perf_counter_ns()
    with tempfile.TemporaryDirectory(prefix="phase4bg-") as temporary:
        target = Path(temporary) / "synthetic-artifact.bin"
        stage = target.with_suffix(".tmp")
        with stage.open("xb") as stream:
            stream.write(artifact)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, target)
        if target.stat().st_size != artifact_bytes:
            raise ValueError("PHASE4BG_PUBLICATION_SIZE_MISMATCH")
    publication_elapsed_ns = time.perf_counter_ns() - publication_started

    rollback_started = time.perf_counter_ns()
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE synthetic(id INTEGER PRIMARY KEY,value TEXT)")
    connection.execute("INSERT INTO synthetic VALUES (1,NULL)")
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE synthetic SET value='temporary' WHERE id=1")
    connection.rollback()
    rollback_verified = (
        connection.execute("SELECT value FROM synthetic WHERE id=1").fetchone()[0] is None
    )
    connection.close()
    rollback_elapsed_ns = time.perf_counter_ns() - rollback_started
    if not rollback_verified or not deterministic_ordering:
        raise ValueError("PHASE4BG_SYNTHETIC_INVARIANT_FAILED")

    evaluated_at = now.astimezone(UTC).isoformat()
    audit: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BG",
        "evaluated_at": evaluated_at,
        "requested": {"rows": rows, "history": history, "artifact_bytes": artifact_bytes},
        "limits": {
            "max_rows": MAX_ROWS,
            "max_history": MAX_HISTORY,
            "max_artifact_bytes": MAX_ARTIFACT_BYTES,
            "max_estimated_memory_bytes": MAX_ESTIMATED_MEMORY_BYTES,
        },
        "measurements": {
            "validation_elapsed_ns": validation_elapsed_ns,
            "publication_elapsed_ns": publication_elapsed_ns,
            "rollback_elapsed_ns": rollback_elapsed_ns,
            "tracemalloc_current_bytes": current,
            "tracemalloc_peak_bytes": peak,
        },
        "ordering_hash": ordering_hash,
        "history_head_hash": head,
        "artifact_hash_measured": artifact_hash,
        "deterministic_ordering_verified": deterministic_ordering,
        "rollback_verified": rollback_verified,
        "synthetic_data_only": True,
        "production_database_mutated": False,
        "execution_authorized": False,
    }
    audit["artifact_hash"] = _hash(audit)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4BG",
        "evaluated_at": evaluated_at,
        "audit_hash": audit["artifact_hash"],
        "limits_checked_before_allocation": True,
        "bounded_temporary_publication": True,
        "rollback_cost_measured": True,
        "network_access_performed": False,
        "production_database_mutated": False,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return audit, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--history", type=int, required=True)
    parser.add_argument("--artifact-bytes", type=int, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    audit, proof = build(
        rows=args.rows, history=args.history, artifact_bytes=args.artifact_bytes, now=now
    )
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.audit_output, args.proof_output, audit, proof)
    print(json.dumps(audit, sort_keys=True))


if __name__ == "__main__":
    main()
