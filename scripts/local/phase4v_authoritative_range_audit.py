"""Read-only authoritative-source and explicit range metadata audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


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
    rows = research.execute(
        """
        SELECT capture_id, ticker, snapshot_id, snapshot_timestamp
        FROM prospective_paired_captures
        WHERE range_comparator_verdict_json IS NOT NULL
        ORDER BY snapshot_timestamp, ticker
        """
    ).fetchall()
    counts: Counter[str] = Counter()
    structures: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for capture in rows:
        snapshot = source.execute(
            "SELECT raw_market_json FROM market_snapshots WHERE id = ?",
            (capture["snapshot_id"],),
        ).fetchone()
        link = source.execute(
            """
            SELECT detected_at, raw_json FROM crypto_market_links
            WHERE ticker = ? AND detected_at <= ?
            ORDER BY detected_at DESC, id DESC LIMIT 1
            """,
            (capture["ticker"], capture["snapshot_timestamp"]),
        ).fetchone()
        if snapshot is None:
            counts["SNAPSHOT_MISSING"] += 1
            continue
        if link is None:
            counts["PRE_CUTOFF_LINK_MISSING"] += 1
            continue
        raw_market = json.loads(snapshot["raw_market_json"] or "{}")
        link_payload = json.loads(link["raw_json"] or "{}")
        terms = link_payload.get("structured_terms") or {}
        structures[str(raw_market.get("strike_type") or "UNKNOWN").upper()] += 1
        fields = {
            "lower_bound": raw_market.get("floor_strike"),
            "upper_bound": raw_market.get("cap_strike"),
            "lower_inclusive": raw_market.get("lower_bound_inclusive"),
            "upper_inclusive": raw_market.get("upper_bound_inclusive"),
            "observation_window_seconds": raw_market.get("observation_window_seconds"),
            "settlement_index_identifier": raw_market.get("settlement_index_identifier"),
            "reference_asset": terms.get("symbol"),
            "settlement_timestamp": terms.get("observation_time"),
            "contract_timezone": terms.get("settlement_timezone"),
            "reference_price_source": terms.get("reference_price_source"),
        }
        for name, value in fields.items():
            disposition = "PRESENT" if value not in (None, "", "unknown") else "MISSING"
            counts[f"{name.upper()}_{disposition}"] += 1
        if len(samples) < 3:
            samples.append(
                {
                    "capture_id": capture["capture_id"],
                    "ticker": capture["ticker"],
                    "snapshot_id": capture["snapshot_id"],
                    "snapshot_timestamp": capture["snapshot_timestamp"],
                    "link_detected_at": link["detected_at"],
                    "raw_market_hash": _hash(raw_market),
                    "structured_terms_hash": _hash(terms),
                    "explicit_fields": fields,
                }
            )
    price_sources = source.execute(
        """
        SELECT source, COUNT(*) AS rows, MIN(observed_at) AS first_at,
               MAX(observed_at) AS last_at
        FROM crypto_prices GROUP BY source ORDER BY source
        """
    ).fetchall()
    output = {
        "schema": "phase4v.authoritative-range-audit.v1",
        "scope": "FORWARD_ONLY_PERSISTED_RANGE_VERDICTS",
        "capture_rows": len(rows),
        "coverage": dict(sorted(counts.items())),
        "contract_structures": dict(sorted(structures.items())),
        "captured_price_sources": [dict(row) for row in price_sources],
        "matching_settlement_index_observations": 0,
        "local_rights_artifacts_for_settlement_index": 0,
        "source_rights_verdict": "RIGHTS_UNPROVEN_DO_NOT_CONNECT",
        "immutable_samples": samples,
    }
    output["audit_hash"] = _hash(output)
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
