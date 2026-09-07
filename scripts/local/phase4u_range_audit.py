"""Read-only Phase 4U audit of immutable forward-lineage captures."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.range_comparator import audit_range_comparator


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--source-db", type=Path, required=True)
    args = parser.parse_args()
    research = _connection(args.research_db)
    source = _connection(args.source_db)
    captures = research.execute(
        """
        SELECT capture_id, snapshot_id, snapshot_timestamp, settlement_target,
               feature_ids_json
        FROM prospective_paired_captures
        WHERE comparator_lineage_json IS NOT NULL
        ORDER BY snapshot_timestamp, ticker
        """
    ).fetchall()
    verdicts: list[dict[str, Any]] = []
    missing_snapshots = 0
    missing_features = 0
    missing_links = 0
    for capture in captures:
        snapshot = source.execute(
            "SELECT ticker, raw_market_json FROM market_snapshots WHERE id = ?",
            (capture["snapshot_id"],),
        ).fetchone()
        feature_ids = json.loads(capture["feature_ids_json"])
        feature_id = next(iter(feature_ids.values()), None)
        feature = source.execute(
            "SELECT * FROM crypto_features WHERE id = ?", (feature_id,)
        ).fetchone()
        if snapshot is None:
            missing_snapshots += 1
            continue
        if feature is None:
            missing_features += 1
            continue
        link = source.execute(
            """
            SELECT raw_json FROM crypto_market_links
            WHERE ticker = ? AND detected_at <= ?
            ORDER BY detected_at DESC, id DESC LIMIT 1
            """,
            (snapshot["ticker"], capture["snapshot_timestamp"]),
        ).fetchone()
        if link is None:
            missing_links += 1
            continue
        raw_market = json.loads(snapshot["raw_market_json"] or "{}")
        link_payload = json.loads(link["raw_json"] or "{}")
        terms = link_payload.get("structured_terms")
        verdicts.append(
            audit_range_comparator(
                raw_market=raw_market,
                structured_terms=terms,
                feature=dict(feature),
                cutoff=datetime.fromisoformat(capture["snapshot_timestamp"]),
                settlement_target=(
                    datetime.fromisoformat(capture["settlement_target"])
                    if capture["settlement_target"]
                    else None
                ),
            )
        )
    output = {
        "scope": "READ_ONLY_IMMUTABLE_FORWARD_LINEAGE_INPUT_AUDIT",
        "capture_rows": len(captures),
        "audited_rows": len(verdicts),
        "missing_snapshots": missing_snapshots,
        "missing_features": missing_features,
        "missing_links": missing_links,
        "verdicts": dict(Counter(row["verdict"] for row in verdicts)),
        "reason_codes": dict(
            sorted(Counter(reason for row in verdicts for reason in row["reason_codes"]).items())
        ),
        "contract_structures": dict(Counter(row["contract_structure"] for row in verdicts)),
        "candidate_probabilities": sum(
            row["candidate_probability"] is not None for row in verdicts
        ),
    }
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
