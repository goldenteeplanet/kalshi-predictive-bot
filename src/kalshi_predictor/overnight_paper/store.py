"""Append-only shadow evidence and separate replay results in an isolated DB.

No exchange client or paper-order creation is imported. Paper activation must
use the separately qualified application ledger and link the identical digest.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.overnight_paper.historical import score_historical

REQUIRED_DECISION = {
    "ticker",
    "event_ticker",
    "series_ticker",
    "model",
    "model_version",
    "forecast",
    "snapshot",
    "price",
    "net_ev",
    "sizing",
    "risk",
    "source_provenance",
    "settlement_rule_version",
    "decision_at",
    "forecast_at",
    "source_updated_at",
    "snapshot_at",
    "close_time",
    "side",
}


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(encode(payload).encode()).hexdigest()


def encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return parsed.astimezone(UTC)


def initialize_store(path: Path) -> None:
    """Only initialize an empty application DB or an already marked sprint DB."""
    if "onedrive" in str(path.absolute()).lower():
        raise ValueError("UNSYNCED_DB_REQUIRED")
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)
    ):
        raise ValueError("LINKED_DB_REFUSED")
    if not path.is_file():
        raise ValueError("EMPTY_APPLICATION_BASELINE_REQUIRED")
    with closing(sqlite3.connect(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {
            "overnight_shadow",
            "overnight_history",
            "overnight_sprint_cycles",
            "weather_settlement_rules",
        } <= tables:
            return
        if not {"paper_orders", "paper_fills", "paper_positions"}.issubset(tables):
            raise ValueError("APPLICATION_SCHEMA_REQUIRED")
        for table in tables:
            escaped = table.replace('"', '""')
            if db.execute(f'SELECT count(*) FROM "{escaped}"').fetchone()[0]:
                raise ValueError("NONEMPTY_UNOWNED_DATABASE_REFUSED")
        db.executescript(
            "BEGIN IMMEDIATE;"
            "CREATE TABLE IF NOT EXISTS overnight_shadow("
            "id TEXT PRIMARY KEY, ticker TEXT NOT NULL, event_ticker TEXT NOT NULL,"
            "decision_at TEXT NOT NULL, payload TEXT NOT NULL, paper_order_id INTEGER UNIQUE,"
            "evaluation_json TEXT);"
            "CREATE TABLE IF NOT EXISTS overnight_history("
            "id TEXT PRIMARY KEY, event_ticker TEXT NOT NULL, payload TEXT NOT NULL,"
            "evaluation_json TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS overnight_sprint_cycles("
            "id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, payload TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS weather_settlement_rules("
            "version TEXT PRIMARY KEY, series TEXT NOT NULL, provider TEXT NOT NULL,"
            "effective_from TEXT, effective_to TEXT, payload TEXT NOT NULL);COMMIT;"
        )


def validate_shadow_source_clock(payload: dict[str, Any]) -> None:
    """Miami's legacy source_updated_at slot means receipt-recorded availability.

    Preserve the explicitly unknown provider timestamp. This is exact shadow
    binding, not proof of public historical availability or model qualification.
    Other sources retain their existing provider-clock contract.
    """
    basis = "miami-original-replay-receipt-v1"
    inputs = payload.get("qualification_inputs", {})
    if not (
        payload.get("series_ticker") == "KXTEMPMIAH"
        or (isinstance(inputs, dict) and inputs.get("source_kind") == "miami-canonical-index-v1")
        or payload.get("source_clock_basis") == basis
    ):
        return
    if not isinstance(inputs, dict):
        raise ValueError("SHADOW_QUALIFICATION_INPUT_TYPE")
    required = {"source_clock_basis", "source_available_at", "source_provider_updated_at"}
    if (
        not required <= payload.keys()
        or payload["source_clock_basis"] != basis
        or payload["source_provider_updated_at"] is not None
        or inputs.get("source_kind") != "miami-canonical-index-v1"
        or payload.get("series_ticker") != inputs.get("series")
        or inputs.get("series") != "KXTEMPMIAH"
    ):
        raise ValueError("MIAMI_SHADOW_CLOCK_BASIS_REQUIRED")
    sources = payload.get("source_provenance")
    if not isinstance(sources, list) or any(not isinstance(s, dict) for s in sources):
        raise ValueError("MIAMI_SHADOW_SOURCE_ORIGINAL_REQUIRED")
    analytical = [s for s in sources if s.get("clock_basis") == basis]
    if len(analytical) != 1:
        raise ValueError("MIAMI_SHADOW_SOURCE_ORIGINAL_REQUIRED")
    source = analytical[0]
    source_hash = digest(source)
    hashes = inputs.get("source_hashes")
    timestamps = inputs.get("source_timestamps")
    if (
        not isinstance(hashes, list)
        or any(type(s) is not str for s in hashes)
        or len(set(hashes)) != len(hashes)
        or source_hash not in hashes
        or not isinstance(timestamps, list)
        or any(not isinstance(t, dict) for t in timestamps)
    ):
        raise ValueError("MIAMI_SHADOW_SOURCE_HASH_BINDING")
    matching = [t for t in timestamps if t.get("sha256") == source_hash]
    expected = {
        key: source.get(key)
        for key in (
            "provider_updated_at",
            "provider_generated_at",
            "available_at",
            "received_at",
            "clock_basis",
        )
    }
    expected["sha256"] = source_hash
    if (
        source.get("role") != "ANALYTICAL_SOURCE"
        or source.get("provider_updated_at") is not None
        or source.get("provider_generated_at") is not None
        or matching != [expected]
        or payload["source_available_at"] != source.get("available_at")
        or payload.get("source_updated_at") != source.get("available_at")
        or not aware(source["received_at"])
        <= aware(source["available_at"])
        <= aware(payload["decision_at"])
    ):
        raise ValueError("MIAMI_SHADOW_RECORDED_AVAILABILITY_BINDING")


def record_shadow(db: sqlite3.Connection, payload: dict[str, Any]) -> str:
    validate_shadow_source_clock(payload)
    if REQUIRED_DECISION - payload.keys():
        raise ValueError("INCOMPLETE_SHADOW_PROVENANCE")
    if any(payload[key] in (None, "", {}) for key in REQUIRED_DECISION):
        raise ValueError("EMPTY_SHADOW_PROVENANCE")
    decision_at = aware(payload["decision_at"])
    if not all(
        aware(payload[k]) <= decision_at
        for k in ("forecast_at", "source_updated_at", "snapshot_at")
    ):
        raise ValueError("FUTURE_INPUT_LEAKAGE")
    if decision_at >= aware(payload["close_time"]):
        raise ValueError("DECISION_AFTER_CLOSE")
    key = digest(payload)
    db.execute(
        "INSERT OR IGNORE INTO overnight_shadow(id,ticker,event_ticker,decision_at,payload) "
        "VALUES(?,?,?,?,?)",
        (key, payload["ticker"], payload["event_ticker"], payload["decision_at"], encode(payload)),
    )
    return key


def verify_shadow(db: sqlite3.Connection, key: str, payload: dict[str, Any]) -> None:
    row = db.execute("SELECT payload FROM overnight_shadow WHERE id=?", (key,)).fetchone()
    if row is None or digest(payload) != key or row[0] != encode(payload):
        raise ValueError("SHADOW_PAPER_SNAPSHOT_MISMATCH")


def link_paper(db: sqlite3.Connection, key: str, payload: dict[str, Any], order_id: int) -> None:
    verify_shadow(db, key, payload)
    existing = db.execute(
        "SELECT paper_order_id FROM overnight_shadow WHERE id=?", (key,)
    ).fetchone()
    if existing[0] not in (None, order_id):
        raise ValueError("SHADOW_ALREADY_ACTIVATED")
    order = db.execute("SELECT ticker FROM paper_orders WHERE id=?", (order_id,)).fetchone()
    if order is None or order[0] != payload["ticker"]:
        raise ValueError("PAPER_ORDER_IDENTITY_MISMATCH")
    db.execute("UPDATE overnight_shadow SET paper_order_id=? WHERE id=?", (order_id, key))


def score_final(payload: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
    """Score final public result only; close/placeholder/sibling linkage is insufficient."""
    if final.get("ticker") != payload["ticker"]:
        raise ValueError("FINAL_IDENTITY_MISMATCH")
    if final.get("status") != "settled" or final.get("result") not in {"yes", "no"}:
        raise ValueError("FINAL_RESULT_REQUIRED")
    if not final.get("source_sha256") or not final.get("settled_at"):
        raise ValueError("FINAL_PROVENANCE_REQUIRED")
    if aware(final["settled_at"]) < aware(payload["close_time"]):
        raise ValueError("FINAL_BEFORE_CLOSE")
    p = Decimal(str(payload["forecast"]))
    if not p.is_finite() or not Decimal(0) <= p <= Decimal(1):
        raise ValueError("INVALID_PROBABILITY")
    outcome = Decimal(final["result"] == "yes")
    brier = (p - outcome) ** 2
    likelihood = p if outcome else 1 - p
    # Preserve impossible-event log loss as infinity text, never clamp an overconfident forecast.
    log_loss = str(-math.log(float(likelihood))) if likelihood > 0 else "Infinity"
    return {
        "result": final["result"],
        "brier": str(brier),
        "log_loss": log_loss,
        "final_source_sha256": final["source_sha256"],
        "settled_at": final["settled_at"],
        "model": payload["model"],
        "event_ticker": payload["event_ticker"],
    }


def record_shadow_evaluation(db: sqlite3.Connection, key: str, final: dict[str, Any]) -> None:
    row = db.execute(
        "SELECT payload,evaluation_json FROM overnight_shadow WHERE id=?", (key,)
    ).fetchone()
    if row is None:
        raise ValueError("SHADOW_REQUIRED")
    evaluated = encode(score_final(json.loads(row[0]), final))
    if row[1] is not None and row[1] != evaluated:
        raise ValueError("FINAL_CORRECTION_REQUIRES_REVIEW")
    db.execute("UPDATE overnight_shadow SET evaluation_json=? WHERE id=?", (evaluated, key))


def record_historical(
    db: sqlite3.Connection, payload: dict[str, Any], final: dict[str, Any]
) -> str:
    if REQUIRED_DECISION - payload.keys():
        raise ValueError("INCOMPLETE_HISTORICAL_PROVENANCE")
    # Replay must use inputs available at the original decision time, never later labels.
    at = aware(payload["decision_at"])
    if at >= aware(payload["close_time"]) or any(
        aware(payload[k]) > at for k in ("forecast_at", "source_updated_at", "snapshot_at")
    ):
        raise ValueError("HISTORICAL_LEAKAGE")
    evaluation = score_final(payload, final)
    historical = score_historical(
        {
            **payload,
            "forecast_probability": payload["forecast"],
            "final_ticker": final.get("ticker"),
            "final_result": final.get("result"),
            "final_status": final.get("status"),
            "final_settled_at": final.get("settled_at"),
            "final_source_sha256": final.get("source_sha256"),
        }
    )
    if historical["status"] != "HISTORICAL_EVALUATED":
        raise ValueError("HISTORICAL_PROVENANCE_REJECTED:" + historical["reason"])
    key = digest(payload)
    db.execute(
        "INSERT OR IGNORE INTO overnight_history VALUES(?,?,?,?)",
        (key, payload["event_ticker"], encode(payload), encode(evaluation)),
    )
    return key
