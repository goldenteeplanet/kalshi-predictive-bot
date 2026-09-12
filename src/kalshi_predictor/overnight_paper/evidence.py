"""Hash-verified diagnostic imports into the isolated evidence tables only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.overnight_paper.settlement import market_lifecycle
from kalshi_predictor.overnight_paper.store import aware, encode, initialize_store


def import_weather_archive(database: Path, archive: Path) -> str:
    """Retain provider clocks and rejected forecasts without promoting them to trading inputs."""
    raw = (archive / "result.json").read_bytes()
    result = json.loads(raw)
    for index, receipt in enumerate(result["requests"], 1):
        if receipt.get("status") != 200:
            raise ValueError("INCOMPLETE_PUBLIC_CAPTURE")
        response = (archive / f"response-{index:02}.json").read_bytes()
        if hashlib.sha256(response).hexdigest() != receipt["sha256"]:
            raise ValueError("PUBLIC_CAPTURE_HASH_MISMATCH")
    if result.get("role") != "DIAGNOSTIC_ONLY" or result.get("max_age_seconds") != 1800:
        raise ValueError("DIAGNOSTIC_BOUNDARY_OR_FRESHNESS_CHANGED")
    key = "weather:" + hashlib.sha256(raw).hexdigest()
    payload = {
        "kind": "WEATHER_DIAGNOSTIC",
        "archive": str(archive.absolute()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "result": result,
        "trading_input": False,
    }
    initialize_store(database)
    with closing(sqlite3.connect(database)) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("DATABASE_INTEGRITY_FAILED")
        for rule in result.get("rules", []):
            db.execute(
                "INSERT OR IGNORE INTO weather_settlement_rules "
                "(version,series,provider,effective_from,effective_to,payload) VALUES(?,?,?,?,?,?)",
                (
                    rule["version"],
                    rule["series"],
                    rule["provider"],
                    rule["effective_from"],
                    rule["effective_to"],
                    encode(rule),
                ),
            )
        db.execute(
            "INSERT OR IGNORE INTO overnight_sprint_cycles(id,captured_at,payload) VALUES(?,?,?)",
            (key, result["finished_at"], encode(payload)),
        )
        db.commit()
    return key


def import_reported_crypto_finals(database: Path, archive: Path) -> str:
    """Store reported final values as diagnostics, never independent benchmark proof or P&L."""
    receipts = json.loads((archive / "requests.json").read_bytes())
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= 10:
        raise ValueError("BOUNDED_PUBLIC_RECEIPTS_REQUIRED")
    examples = []
    for index, receipt in enumerate(receipts, 1):
        raw = (archive / f"response-{index:03}.json").read_bytes()
        if (
            receipt.get("method") != "GET"
            or receipt.get("status") != 200
            or hashlib.sha256(raw).hexdigest() != receipt.get("sha256")
        ):
            raise ValueError("PUBLIC_CAPTURE_HASH_MISMATCH")
        if receipt.get("params") != {"series_ticker": "KXBTC15M", "status": "settled", "limit": 5}:
            continue
        if receipt["url"] != "https://external-api.kalshi.com/trade-api/v2/markets":
            raise ValueError("PUBLIC_MARKET_ENDPOINT_REQUIRED")
        markets = json.loads(raw)["markets"]
        if not 1 <= len(markets) <= 5:
            raise ValueError("BOUNDED_FINAL_SAMPLE_REQUIRED")
        for market in markets:
            if not market["ticker"].startswith("KXBTC15M-"):
                raise ValueError("FINAL_SERIES_MISMATCH")
            state = market_lifecycle(market["ticker"], market, now=aware(receipt["received_at"]))
            if state["final"] is None or market.get("strike_type") != "greater_or_equal":
                raise ValueError("SUPPORTED_FINAL_METHOD_REQUIRED")
            value, threshold = (
                Decimal(market["expiration_value"]),
                Decimal(str(market["floor_strike"])),
            )
            if not value.is_finite() or not threshold.is_finite():
                raise ValueError("FINITE_FINAL_VALUE_REQUIRED")
            reproduced = "yes" if value >= threshold else "no"
            if reproduced != market["result"]:
                raise ValueError("REPORTED_FINAL_RESULT_CONFLICT")
            examples.append(
                {
                    "market": market,
                    "final": state["final"],
                    "reproduced_reported_result": reproduced,
                    "independent_cf_verified": False,
                }
            )
    if not examples:
        raise ValueError("PUBLIC_FINAL_SAMPLE_MISSING")
    payload = {
        "kind": "CRYPTO_FINAL_DIAGNOSTIC",
        "archive": str(archive.absolute()),
        "receipts": receipts,
        "examples": examples,
        "trading_input": False,
        "independent_source_reproductions": 0,
    }
    encoded = encode(payload)
    key = "crypto-final:" + hashlib.sha256(encoded.encode()).hexdigest()
    initialize_store(database)
    with closing(sqlite3.connect(database)) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("DATABASE_INTEGRITY_FAILED")
        db.execute(
            "INSERT OR IGNORE INTO overnight_sprint_cycles(id,captured_at,payload) VALUES(?,?,?)",
            (key, max(r["received_at"] for r in receipts), encoded),
        )
        db.commit()
    return key
