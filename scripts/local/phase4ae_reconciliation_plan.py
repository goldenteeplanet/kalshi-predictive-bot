"""Phase 4AE deterministic read-only prospective reconciliation planner."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.ingest.settlement_hints import artifact_hash as hint_artifact_hash
from kalshi_predictor.phase4cd.reconciliation_audit import (
    canonical_hash,
    capture_lineage_hash,
    normalized_timestamp,
    prospective_evaluation_values,
    settlement_lineage_hash,
)

SCHEMA = "phase4ae.reconciliation-plan.v1"
SOURCE_SCHEMA = "phase4ad.reconciliation-attribution.v1"


def _utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=1")
    return connection


def artifact_hash(payload: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def load_attribution(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SOURCE_SCHEMA:
        raise ValueError("PHASE4AE_ATTRIBUTION_SCHEMA_INVALID")
    if payload.get("artifact_hash") != artifact_hash(payload):
        raise ValueError("PHASE4AE_ATTRIBUTION_HASH_MISMATCH")
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_hash") != canonical_hash(rows):
        raise ValueError("PHASE4AE_ATTRIBUTION_ROWS_HASH_MISMATCH")
    return payload


def _load_history_manifest_hash(history_dir: Path) -> str:
    path = Path(__file__).with_name("phase4ac_artifact_history.py")
    spec = importlib.util.spec_from_file_location("phase4ac_artifact_history_for_4ae", path)
    if not spec or not spec.loader:
        raise ValueError("PHASE4AE_PHASE4AC_LOADER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reconstructed = module.reconstruct_phase4ab_history(history_dir)
    return str(reconstructed["manifest_hash"])


def _validate_hint(path: Path, expected_hash: str) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("artifact_hash") != hint_artifact_hash(payload):
        raise ValueError("PHASE4AE_HINT_HASH_MISMATCH")
    if payload["artifact_hash"] != expected_hash:
        raise ValueError("PHASE4AE_HINT_LINEAGE_MISMATCH")
    return str(payload["artifact_hash"])


def _planned_row(
    capture: sqlite3.Row,
    settlement: sqlite3.Row,
    *,
    fees: Decimal,
    slippage: Decimal,
) -> dict[str, Any]:
    values = prospective_evaluation_values(
        dict(capture), dict(settlement), fees=fees, slippage=slippage
    )
    row: dict[str, Any] = {
        "disposition": "PLANNED",
        "evaluation_id": values["evaluation_id"],
        "capture_id": values["capture_id"],
        "ticker": capture["ticker"],
        "independent_event_id": values["independent_event_id"],
        "settlement_hash": values["settlement_hash"],
        "settlement_timestamp": normalized_timestamp(values["settled_at"]),
        "settlement_updated_at": normalized_timestamp(values["settlement_updated_at"]),
        "snapshot_timestamp": normalized_timestamp(capture["snapshot_timestamp"]),
        "outcome": values["outcome"],
        "market_probability": values["market_probability"],
        "model_probability": values["model_probability"],
        "market_brier": values["market_brier"],
        "model_brier": values["model_brier"],
        "market_log_loss": values["market_log_loss"],
        "model_log_loss": values["model_log_loss"],
        "probability_advantage": values["probability_advantage"],
        "crossing_spread_cost": values["crossing_spread_cost"],
        "fees": values["fees"],
        "slippage": values["slippage"],
        "gross_edge": values["gross_edge"],
        "net_edge": values["net_edge"],
        "terminal_reason": values["terminal_reason"],
        "hypothetical_pnl": values["hypothetical_pnl"],
        "capture_lineage_hash": values["capture_lineage_hash"],
    }
    row["planned_record_hash"] = canonical_hash(row)
    return row


def _order_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    planned = [row for row in rows if row["disposition"] == "PLANNED"]
    planned.sort(
        key=lambda row: (
            _utc(row["settlement_timestamp"]),
            _utc(row["snapshot_timestamp"]),
            str(row["ticker"]),
            str(row["capture_id"]),
        )
    )
    other = [row for row in rows if row["disposition"] != "PLANNED"]
    other.sort(key=lambda row: (str(row.get("ticker")), str(row.get("capture_id"))))
    return planned + other


def plan(
    source_db: Path,
    research_db: Path,
    attribution_path: Path,
    history_dir: Path,
    hint_path: Path,
    *,
    now: datetime,
    maximum_age_seconds: int,
    fees: Decimal = Decimal("0"),
    slippage: Decimal = Decimal("0"),
) -> dict[str, Any]:
    if maximum_age_seconds < 0:
        raise ValueError("PHASE4AE_MAXIMUM_AGE_INVALID")
    attribution = load_attribution(attribution_path)
    manifest_hash = _load_history_manifest_hash(history_dir)
    if attribution["source_phase4ac_manifest_hash"] != manifest_hash:
        raise ValueError("PHASE4AE_PHASE4AC_LINEAGE_MISMATCH")
    hint_hash = _validate_hint(hint_path, attribution["source_hint_artifact_hash"])
    age = (now.astimezone(UTC) - _utc(attribution["generated_at"])).total_seconds()
    stale = age < 0 or age >= maximum_age_seconds
    source, research = _ro(source_db), _ro(research_db)
    rows: list[dict[str, Any]] = []
    for attributed in attribution["rows"]:
        if attributed.get("classification") != "READY_FOR_RECONCILIATION":
            rows.append(
                {
                    "capture_id": attributed.get("capture_id"),
                    "ticker": attributed.get("ticker"),
                    "disposition": "EXCLUDED",
                    "source_classification": attributed.get("classification"),
                }
            )
            continue
        capture_id = attributed.get("capture_id")
        ticker = attributed.get("ticker")
        try:
            capture = research.execute(
                "SELECT * FROM prospective_paired_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            settlement = source.execute(
                "SELECT * FROM settlements WHERE ticker=?", (ticker,)
            ).fetchone()
            evaluation = research.execute(
                "SELECT capture_id FROM prospective_pair_evaluations WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
            if stale:
                row = {
                    "capture_id": capture_id,
                    "ticker": ticker,
                    "disposition": "ATTRIBUTION_STALE",
                }
            elif evaluation is not None:
                row = {
                    "capture_id": capture_id,
                    "ticker": ticker,
                    "disposition": "EVALUATION_APPEARED",
                }
            elif capture is None or settlement is None:
                row = {"capture_id": capture_id, "ticker": ticker, "disposition": "SOURCE_MISSING"}
            elif settlement_lineage_hash(dict(settlement)) != attributed["settlement_hash"]:
                row = {
                    "capture_id": capture_id,
                    "ticker": ticker,
                    "disposition": "SETTLEMENT_DRIFTED",
                }
            elif capture_lineage_hash(dict(capture)) != attributed["capture_lineage_hash"]:
                row = {"capture_id": capture_id, "ticker": ticker, "disposition": "CAPTURE_DRIFTED"}
            else:
                row = _planned_row(capture, settlement, fees=fees, slippage=slippage)
        except Exception as error:  # bounded, fail-closed row disposition
            row = {
                "capture_id": capture_id,
                "ticker": ticker,
                "disposition": "PLAN_BLOCKED",
                "error_code": type(error).__name__,
            }
        rows.append(row)
    source.close()
    research.close()
    rows = _order_rows(rows)
    planned = [row for row in rows if row["disposition"] == "PLANNED"]
    counts = Counter(row["disposition"] for row in rows)
    input_count = len(attribution["rows"])
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": now.astimezone(UTC).isoformat(),
        "source_mode": "ro/query_only",
        "research_mode": "ro/query_only",
        "source_phase4ad_artifact_hash": attribution["artifact_hash"],
        "source_phase4ac_manifest_hash": manifest_hash,
        "source_phase4aa_artifact_hash": attribution["source_phase4aa_artifact_hash"],
        "source_hint_artifact_hash": hint_hash,
        "maximum_attribution_age_seconds": maximum_age_seconds,
        "attribution_age_seconds": age,
        "input_row_count": input_count,
        "planned_row_count": counts["PLANNED"],
        "excluded_row_count": input_count - counts["PLANNED"],
        "disposition_counts": dict(sorted(counts.items())),
        "independent_event_count": len({row["independent_event_id"] for row in planned}),
        "ticker_count": len({row["ticker"] for row in planned}),
        "safe_for_research_apply": bool(planned) and len(planned) == input_count,
        "production_database_written": False,
        "research_database_written": False,
        "trading_mode_changed": False,
        "rows_hash": canonical_hash(rows),
        "rows": rows,
    }
    payload["artifact_hash"] = artifact_hash(payload)
    return payload


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--attribution-artifact", type=Path, required=True)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--hint-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-age-seconds", type=int, default=1800)
    args = parser.parse_args()
    result = plan(
        args.source_db,
        args.research_db,
        args.attribution_artifact,
        args.history_dir,
        args.hint_artifact,
        now=datetime.now(UTC),
        maximum_age_seconds=args.maximum_age_seconds,
    )
    write_atomic(args.output, result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
